"""The i.MX RT1060 backend: drive ``sdphost`` + ``blhost`` (NXP spsdk).

Unlike the DFU boards, the RT1062 has no resident DFU bootloader -- it's flashed through the
ROM's serial-download protocol (SDP). The flow, mirroring the OpenMV IDE's ``imx.cpp``:

1. ``sdphost`` loads a RAM **flashloader** (``sdphost_flash_loader.bin``) and jumps to it.
2. The flashloader re-enumerates as the MCU-bootloader (blhost) device; we settle, then poll
   ``blhost get-property 1`` until it answers (the "sync while the bootloader is up" step).
3. ``blhost`` configures the FlexSPI NOR, then erases/writes each region. A full ``factory``
   flash also writes the flash-config block (FCB), the secure bootloader, and burns the boot
   e-fuse; a ``firmware``/``romfs`` update just rewrites that one region.
4. ``blhost reset`` runs the new image.

Every command is a pure argv (testable); the only runtime subtlety -- the post-jump poll --
is a ``probe`` step the orchestrator retries. The flashloader binaries are prebuilt artifacts
(shipped with the firmware/IDE, not produced by ``build``), resolved from the flashloader dir.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ERASE_TIMEOUT_MS = 120000        # the IDE allows 120s for an erase
_SECTOR = 0x1000                 # FlexSPI NOR erase granularity; round erase lengths up to it


@dataclass(frozen=True)
class ImxStep:
    label: str
    argv: list[str]
    probe: bool = False          # a probe step is polled until it succeeds (the post-jump sync)

    @property
    def summary(self) -> str:
        return self.label


def _sdphost(sdphost: str, usb: str, *sub: str) -> list[str]:
    return [sdphost, "-u", usb, "--", *sub]


def _blhost(blhost: str, usb: str, *sub: str, timeout: int | None = None) -> list[str]:
    argv = [blhost, "-u", usb]
    if timeout is not None:
        argv += ["-t", str(timeout)]
    return argv + ["--", *sub]


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


def plan(op: str, raw: dict, sdphost: str, blhost: str, files: dict[str, Path]) -> list[ImxStep]:
    """The ordered command list for an i.MX ``op`` (``firmware``/``romfs``/``factory``).

    ``files`` holds the resolved paths the op needs: ``sdphost_loader`` always, plus
    ``blhost_loader``/``firmware``/``romfs`` as the op requires.
    """
    sd, bl = raw["sdphost"], raw["blhost"]
    usb = bl["usb"]

    steps = [
        ImxStep("load flashloader -> %s" % sd["loader_addr"],
                _sdphost(sdphost, sd["usb"], "write-file", sd["loader_addr"],
                         str(files["sdphost_loader"]))),
        ImxStep("jump to flashloader",
                _sdphost(sdphost, sd["usb"], "jump-address", sd["loader_addr"])),
        ImxStep("wait for the flashloader",
                _blhost(blhost, usb, "get-property", "1"), probe=True),
        ImxStep("configure FlexSPI NOR",
                _blhost(blhost, usb, "fill-memory", bl["cfg_addr"], "4", bl["cfg_spi"], "word")),
        ImxStep("apply FlexSPI config",
                _blhost(blhost, usb, "configure-memory", bl["cfg_type"], bl["cfg_addr"])),
    ]

    if op == "factory":                                # write the FCB so the ROM can boot FlexSPI
        steps += [
            ImxStep("erase FCB %s" % bl["fcb_addr"],
                    _blhost(blhost, usb, "flash-erase-region", bl["fcb_addr"], bl["fcb_len"],
                            timeout=ERASE_TIMEOUT_MS)),
            ImxStep("configure FCB",
                    _blhost(blhost, usb, "fill-memory", bl["cfg_addr"], "4", bl["cfg_fcb"], "word")),
            ImxStep("apply FCB config",
                    _blhost(blhost, usb, "configure-memory", bl["cfg_type"], bl["cfg_addr"])),
        ]
        steps += _write_region(blhost, usb, bl["sbl_addr"], files["blhost_loader"])
        steps += _write_region(blhost, usb, bl["firmware_addr"], files["firmware"])
        steps += _write_region(blhost, usb, bl["romfs_addr"], files["romfs"])
        steps.append(ImxStep("burn boot e-fuse",
                             _blhost(blhost, usb, "efuse-program-once",
                                     bl["efuse_addr"], bl["efuse_data"])))
    elif op == "firmware":
        steps += _write_region(blhost, usb, bl["firmware_addr"], files["firmware"])
    else:                                              # romfs
        steps += _write_region(blhost, usb, bl["romfs_addr"], files["romfs"])

    steps.append(ImxStep("reset", _blhost(blhost, usb, "reset")))
    return steps
