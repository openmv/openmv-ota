"""The ``dfu-util`` backend: build the argv to read/write one DFU alt-setting.

OpenMV's bootloader exposes each flashable partition as a numbered DFU **alt-setting**
(the index into the board's ``OMV_BOOT_DFU_PARTITIONS`` table). dfu-util addresses a
partition by that alt -- the bootloader maps block offsets to absolute flash addresses
itself, so the host only needs the alt, not the address.

The argv mirrors what the OpenMV IDE issues: ``-w`` (wait for the device to appear, since
DFU re-enumeration is racy), ``-d ,<vid:pid>`` (match the device in DFU mode -- the leading
comma scopes the id to the DFU-mode descriptor), and ``--reset`` on the *final* step of a
flash so the board reboots only after the last write (it stays in the bootloader between the
steps of a multi-partition flash).
"""

from __future__ import annotations

from pathlib import Path


def download_argv(dfu_util: str, usb: str, alt: int, file: Path, *, reset: bool = True,
                  serial: str | None = None) -> list[str]:
    """Argv to flash ``file`` to DFU alt ``alt`` on the ``vid:pid`` device ``usb``. ``serial``
    pins it to one specific board (``-S``) when several are in DFU at once."""
    argv = [dfu_util, "-w", "-d", ",%s" % usb]
    if serial:
        argv += ["-S", serial]
    argv += ["-a", str(alt)]
    if reset:
        argv.append("--reset")
    argv += ["-D", str(file)]
    return argv


def upload_argv(dfu_util: str, usb: str, alt: int, file: Path) -> list[str]:
    """Argv to read DFU alt ``alt`` back into ``file`` (for a post-flash verify)."""
    return [dfu_util, "-w", "-d", ",%s" % usb, "-a", str(alt), "-U", str(file)]


def bootloader_argv(dfu_util: str, usb: str, alt: int, addr: str, file: Path, *,
                    serial: str | None = None) -> list[str]:
    """Argv to write the bootloader to absolute address ``addr`` via the **system** DFU
    (``usb`` = e.g. 0483:df11). No ``--reset``/``:leave``: the ST system ROM doesn't ACK the
    final status, so the caller tolerates a non-zero exit (matching the IDE)."""
    argv = [dfu_util, "-w", "-d", ",%s" % usb]
    if serial:
        argv += ["-S", serial]
    return argv + ["-a", str(alt), "-s", addr, "-D", str(file)]
