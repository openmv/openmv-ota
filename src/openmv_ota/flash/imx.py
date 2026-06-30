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
    """The ordered command list for an i.MX ``op`` (``firmware``/``romfs``/``factory``/
    ``bootloader``). ``files`` holds the resolved paths the op needs: ``sdphost_loader`` always,
    plus ``blhost_loader``/``firmware``/``romfs`` as the op requires."""
    sd, bl = raw["sdphost"], raw["blhost"]
    usb = bl["usb"]

    steps = []
    if op == "bootloader":             # manual SBL/recovery entry -> wait for the ROM device
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
        ImxStep("configure FlexSPI NOR",
                _blhost(blhost, usb, "fill-memory", bl["cfg_addr"], "4", bl["cfg_spi"], "word")),
        ImxStep("apply FlexSPI config",
                _blhost(blhost, usb, "configure-memory", bl["cfg_type"], bl["cfg_addr"])),
    ]

    if op in ("factory", "bootloader"):                # the FCB + the secure bootloader (SBL)
        steps += _fcb(blhost, usb, bl)
        steps += _write_region(blhost, usb, bl["sbl_addr"], files["blhost_loader"])
    if op == "factory":                                # plus firmware, romfs, and the boot e-fuse
        steps += _write_region(blhost, usb, bl["firmware_addr"], files["firmware"])
        steps += _write_region(blhost, usb, bl["romfs_addr"], files["romfs"])
        steps.append(ImxStep("burn boot e-fuse",
                             _blhost(blhost, usb, "efuse-program-once",
                                     bl["efuse_addr"], bl["efuse_data"])))
    elif op == "firmware":
        steps += _write_region(blhost, usb, bl["firmware_addr"], files["firmware"])
    elif op == "romfs":
        steps += _write_region(blhost, usb, bl["romfs_addr"], files["romfs"])

    steps.append(ImxStep("reset", _blhost(blhost, usb, "reset")))
    return steps
