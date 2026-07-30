"""The i.MX RT1060 backend: drive ``sdphost`` + ``blhost`` (NXP spsdk).

Unlike the DFU boards, the RT1062 has no resident DFU bootloader -- it's flashed through the
ROM's serial-download protocol (SDP). The flow, mirroring the OpenMV IDE's ``imx.cpp``:

1. ``sdphost`` loads a RAM **flashloader** (``sdphost_flash_loader.bin``) and jumps to it.
2. The flashloader re-enumerates as the MCU-bootloader (blhost) USB device. We **wait** for it
   to appear -- one process that polls spsdk's USB scan internally (like ``dfu-util -w``),
   instead of relaunching ``blhost`` to retry ``get-property`` (a heavy, flaky poll).
3. ``blhost`` configures the FlexSPI NOR, then erases/writes each region. A full ``factory``
   flash also writes the flash-config block (FCB), the secure bootloader, and burns the boot
   e-fuse; a ``firmware``/``romfs`` update just rewrites that one region.
4. ``blhost reset`` runs the new image.

Every command is a pure argv (testable). The flashloader binaries are prebuilt artifacts
(shipped with the firmware/IDE, not produced by ``build``), resolved from the flashloader dir.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ERASE_TIMEOUT_MS = 120000        # the IDE allows 120s for an erase
WAIT_TIMEOUT_S = 30              # wait for the flashloader to enumerate after the jump
SDP_WAIT_TIMEOUT_S = 120         # wait for the ROM device (the user enters SBL recovery by hand)
_SECTOR = 0x1000                 # FlexSPI NOR erase granularity; round erase lengths up to it

# A single process that waits for an spsdk USB device to enumerate, polling its scan in-process
# -- the dfu-util ``-w`` equivalent. Run with the SDK's python (where spsdk lives); argv:
# <python3> -c <this> <module> <class> <vid,pid> <timeout_s>. Exits 0 once present, 1 on timeout.
_WAIT_SCRIPT = (
    "import sys, time, importlib\n"
    "mod, cls, dev, t = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])\n"
    "scan = getattr(importlib.import_module(mod), cls).scan\n"
    "deadline = time.time() + t\n"
    "while time.time() < deadline:\n"
    "    if scan(device_id=dev):\n"
    "        sys.exit(0)\n"
    "    time.sleep(0.2)\n"
    "sys.stderr.write('i.MX device %s did not enumerate within %ss\\n' % (dev, sys.argv[4]))\n"
    "sys.exit(1)\n"
)
_MBOOT_IF = ("spsdk.mboot.interfaces.usb", "MbootUSBInterface")   # the flashloader (post-jump)
_SDP_IF = ("spsdk.sdp.interfaces.usb", "SdpUSBInterface")         # the ROM (serial download)

# The resident SBL's CURRENT_VERSION property (K2.8.0 = 0x4B020800), the flashloader identity the
# IDE's imxGetDevice() accepts. The catcher only reports CLAIMED when get-property returns this.
SBL_EXPECTED_VERSION = 0x4B020800

# The ACTIVE resident-SBL "catcher" -- the openmv-ota port of the OpenMV IDE's IMX_CATCHER_SCRIPT
# (qt-creator .../openmv/tools/imx.cpp). Passive scanning ``after`` a reset misses the SBL: the
# ~1 s idle window closes while spsdk is still importing, and MbootUSBInterface.scan() stalls up to
# 10 s on the spsdk device-DB FileLock. Instead we ARM this BEFORE the reset -- it warms libusbsio,
# prints READY (the slow import happens off the device's critical window), then hot-loops scanning
# by VID:PID DIRECTLY (UsbDevice.scan, no DB, no lock) and, the instant the SBL enumerates, CLAIMS
# it with a get-property so the idle timeout can't drop it back to runtime before we flash. Argv:
# <python3> -c <this> <mode:wait|claim> <vid,pid> <timeout_s> <version>.
_CATCHER_SCRIPT = (
    "import sys, time, importlib\n"
    "mode, dev, t, want = sys.argv[1], sys.argv[2], float(sys.argv[3]), int(sys.argv[4])\n"
    "Mboot = importlib.import_module('spsdk.mboot.mcuboot').McuBoot\n"
    "Iface = importlib.import_module('spsdk.mboot.interfaces.usb').MbootUSBInterface\n"
    "UsbDevice = importlib.import_module('spsdk.utils.interfaces.device.usb_device').UsbDevice\n"
    "def find():\n"
    "    return [Iface(d) for d in UsbDevice.scan(device_id=dev)]\n"
    "try:\n"
    "    find()\n"                                        # warm libusbsio/HIDAPI before READY
    "except Exception:\n"
    "    pass\n"
    "print('READY', flush=True)\n"
    "deadline = (time.time() + t) if t > 0 else None\n"
    "while deadline is None or time.time() < deadline:\n"
    "    ifaces = find()\n"
    "    if ifaces:\n"
    "        if mode == 'wait':\n"
    "            print('FOUND', flush=True); sys.exit(0)\n"
    "        grace = time.time() + 1.0\n"
    "        while time.time() < grace:\n"
    "            try:\n"
    "                with Mboot(ifaces[0]) as mb:\n"
    "                    vals = mb.get_property(1)\n"
    "                    if vals and vals[0] == want:\n"
    "                        print('CLAIMED %d' % vals[0], flush=True); sys.exit(0)\n"
    "            except Exception:\n"
    "                pass\n"
    "            ifaces = find() or ifaces\n"
    "            time.sleep(0.02)\n"
    "    time.sleep(0.05)\n"
    "sys.stderr.write('imx catcher: %s did not enumerate within %ss\\n' % (dev, sys.argv[3]))\n"
    "sys.exit(2)\n"
)


def catcher_argv(python3: str, pidvid: str, mode: str = "claim",
                 timeout_s: float = 30, version: int = SBL_EXPECTED_VERSION) -> list[str]:
    """Argv for the resident-SBL catcher (see ``_CATCHER_SCRIPT``). Arm it, wait for it to print
    READY, THEN reset the board; it prints CLAIMED (mode 'claim', holds the SBL) or FOUND (mode
    'wait') once the SBL enumerates. Run with the SDK python (where spsdk lives)."""
    return [python3, "-c", _CATCHER_SCRIPT, mode, pidvid, "%g" % timeout_s, str(version)]

# One process that scans (once) for each spsdk USB id and prints ``FOUND <id>`` for those
# present -- the read-only counterpart of the wait script, for ``flash list``. Run with the
# SDK's python; argv: <python3> -c <this> <mod>|<cls>|<id> ...
_SCAN_SCRIPT = (
    "import sys, importlib\n"
    "for spec in sys.argv[1:]:\n"
    "    mod, cls, dev = spec.split('|')\n"
    "    if getattr(importlib.import_module(mod), cls).scan(device_id=dev):\n"
    "        sys.stdout.write('FOUND ' + dev + '\\n')\n"
)


def scan_argv(python3: str, specs: list[tuple[str, str, str]]) -> list[str]:
    """Argv to scan for each ``(module, class, device_id)`` spsdk USB device in one process."""
    return [python3, "-c", _SCAN_SCRIPT] + ["|".join(s) for s in specs]


@dataclass(frozen=True)
class ImxStep:
    label: str
    argv: list[str]


def _sdphost(sdphost: str, usb: str, *sub: str) -> list[str]:
    return [sdphost, "-u", usb, "--", *sub]


def _blhost(blhost: str, usb: str, *sub: str, timeout: int | None = None) -> list[str]:
    argv = [blhost, "-u", usb]
    if timeout is not None:
        argv += ["-t", str(timeout)]
    return argv + ["--", *sub]


def _wait_argv(python3: str, usb: str, *, sdp: bool = False) -> list[str]:
    mod, cls = _SDP_IF if sdp else _MBOOT_IF
    timeout = SDP_WAIT_TIMEOUT_S if sdp else WAIT_TIMEOUT_S
    return [python3, "-c", _WAIT_SCRIPT, mod, cls, usb, "%g" % timeout]


def _aligned(size: int) -> int:
    return (size + _SECTOR - 1) & ~(_SECTOR - 1)


def _write_region(blhost: str, usb: str, addr: str, file: Path) -> list[ImxStep]:
    length = "0x%X" % _aligned(file.stat().st_size)
    return [
        ImxStep("erase %s (%s)" % (addr, length),
                _blhost(blhost, usb, "flash-erase-region", addr, length, timeout=ERASE_TIMEOUT_MS)),
        ImxStep("write %s -> %s" % (file.name, addr),
                _blhost(blhost, usb, "write-memory", addr, str(file))),
    ]


def _fcb(blhost: str, usb: str, bl: dict) -> list[ImxStep]:
    """Write the flash-config block so the ROM can boot from the FlexSPI NOR."""
    return [
        ImxStep("erase FCB %s" % bl["fcb_addr"],
                _blhost(blhost, usb, "flash-erase-region", bl["fcb_addr"], bl["fcb_len"],
                        timeout=ERASE_TIMEOUT_MS)),
        ImxStep("configure FCB",
                _blhost(blhost, usb, "fill-memory", bl["cfg_addr"], "4", bl["cfg_fcb"], "word")),
        ImxStep("apply FCB config",
                _blhost(blhost, usb, "configure-memory", bl["cfg_type"], bl["cfg_addr"])),
    ]


def plan(op: str, raw: dict, sdphost: str, blhost: str, python3: str,
         files: dict[str, Path]) -> list[ImxStep]:
    """The ordered command list for an i.MX ``op``.

    Two entry paths:

    * **Automatable** (``firmware`` / ``romfs`` / ``erase``) -- drive the board's *resident* secure
      bootloader (the MCU-bootloader / blhost device), which ``machine.bootloader()`` enters with NO
      jumper. Because it runs *after* the ROM has already applied the flash-config block (FCB), the
      FlexSPI NOR is configured, so there is no ``sdphost`` flashloader load and no config-register
      writes -- straight to the region op. This is the everyday update path (and what the HIL bench
      uses).
    * **Recovery** (``factory`` / ``bootloader``) -- these rewrite the SBL (and FCB) itself, so they
      cannot rely on the resident SBL. They load a RAM flashloader over the ROM's serial-download
      protocol (SDP); that fresh flashloader starts with FlexSPI *unconfigured*, so it must be
      configured first. SDP requires the board manually in ROM-serial-download (the SBL jumper) --
      inherently not automatable, mirroring the OpenMV IDE, which only writes the config registers on
      a factory provision.

    ``files`` holds the resolved paths the op needs (``sdphost_loader`` + ``blhost_loader`` only for
    the recovery path; ``firmware`` / ``romfs`` as the op requires)."""
    sd, bl = raw["sdphost"], raw["blhost"]
    usb = bl["usb"]
    steps: list[ImxStep] = []

    if op in ("factory", "bootloader"):
        steps.append(ImxStep("wait for the ROM (SDP) device",
                             _wait_argv(python3, sd["usb"], sdp=True)))
        steps += [
            ImxStep("load flashloader -> %s" % sd["loader_addr"],
                    _sdphost(sdphost, sd["usb"], "write-file", sd["loader_addr"],
                             str(files["sdphost_loader"]))),
            ImxStep("jump to flashloader",
                    _sdphost(sdphost, sd["usb"], "jump-address", sd["loader_addr"])),
            ImxStep("wait for the flashloader to enumerate",
                    _wait_argv(python3, usb)),
            ImxStep("configure FlexSPI NOR",             # the SDP-loaded flashloader starts unconfigured
                    _blhost(blhost, usb, "fill-memory", bl["cfg_addr"], "4", bl["cfg_spi"], "word")),
            ImxStep("apply FlexSPI config",
                    _blhost(blhost, usb, "configure-memory", bl["cfg_type"], bl["cfg_addr"])),
        ]
        steps += _fcb(blhost, usb, bl)                   # the FCB + the secure bootloader (SBL)
        steps += _write_region(blhost, usb, bl["sbl_addr"], files["blhost_loader"])
        if op == "factory":                              # plus firmware, romfs, and the boot e-fuse
            steps += _write_region(blhost, usb, bl["firmware_addr"], files["firmware"])
            steps += _write_region(blhost, usb, bl["romfs_addr"], files["romfs"])
            steps.append(ImxStep("burn boot e-fuse",
                                 _blhost(blhost, usb, "efuse-program-once",
                                         bl["efuse_addr"], bl["efuse_data"])))
    else:
        # Resident SBL: the caller has already ENTERED + CLAIMED it (the catcher, see flash.py --
        # armed before the reset, holds it against the ~1 s idle timeout), and it's FlexSPI-configured
        # from the FCB. So no SDP load, no wait step here, and no config-register writes -- straight
        # to the region op (then reset). blhost runs back-to-back so the SBL never idles out.
        if op == "firmware":
            steps += _write_region(blhost, usb, bl["firmware_addr"], files["firmware"])
        elif op == "romfs":
            steps += _write_region(blhost, usb, bl["romfs_addr"], files["romfs"])
        elif op == "erase":                              # wipe the user disk's first sector (its MBR)
            steps.append(ImxStep("erase disk %s (%s)" % (bl["disk_addr"], bl["disk_size"]),
                                 _blhost(blhost, usb, "flash-erase-region", bl["disk_addr"],
                                         bl["disk_size"], timeout=ERASE_TIMEOUT_MS)))
        elif op == "erase_romfs":                        # wipe the whole OTA romfs region (both slots)
            steps.append(ImxStep("erase romfs %s (%s)" % (bl["romfs_addr"], bl["romfs_size"]),
                                 _blhost(blhost, usb, "flash-erase-region", bl["romfs_addr"],
                                         bl["romfs_size"], timeout=ERASE_TIMEOUT_MS)))

    steps.append(ImxStep("reset", _blhost(blhost, usb, "reset")))
    return steps
