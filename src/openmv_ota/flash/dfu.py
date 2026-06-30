"""The ``dfu-util`` backend: build the argv to read/write one DFU alt-setting.

OpenMV's bootloader exposes each flashable partition as a numbered DFU **alt-setting**
(the index into the board's ``OMV_BOOT_DFU_PARTITIONS`` table). dfu-util addresses a
partition by that alt -- the bootloader maps block offsets to absolute flash addresses
itself, so the host only needs the alt, not the address.

``-s :leave`` makes the device exit DFU and boot the new image after a write; a multi-step
flash (firmware then romfs) leaves only on the final step so the device stays in DFU
between writes.
"""

from __future__ import annotations

from pathlib import Path


def download_argv(dfu_util: str, usb: str, alt: int, file: Path, *, leave: bool = True
                  ) -> list[str]:
    """Argv to flash ``file`` to DFU alt ``alt`` on the ``vid:pid`` device ``usb``."""
    argv = [dfu_util, "-d", usb, "-a", str(alt), "-D", str(file)]
    if leave:
        argv += ["-s", ":leave"]
    return argv


def upload_argv(dfu_util: str, usb: str, alt: int, file: Path) -> list[str]:
    """Argv to read DFU alt ``alt`` back into ``file`` (for a post-flash verify)."""
    return [dfu_util, "-d", usb, "-a", str(alt), "-U", str(file)]
