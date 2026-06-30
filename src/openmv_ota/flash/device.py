"""Find a connected camera and get it into its bootloader before flashing.

A flash backend talks to a board that's *already* in its bootloader. A camera you just
plugged in, though, is running its firmware -- enumerated as a USB serial (VCP) device at its
**runtime** VID:PID. This module scans for that, resets it into the bootloader, and reports
the USB serial number so the flash command can target that exact board (``dfu-util -S``) when
several are attached.

Two reset paths to the same place:
- **OpenMV-protocol boards** run ``machine.bootloader()`` via ``mpremote`` -- the firmware
  writes the OpenMV boot magic and resets into its own DFU (37c5:9xxx), not the ST one.
- **Arduino boards** take a 1200-baud serial touch (the MCUboot reset signal).

A board already in its bootloader isn't a serial port, so it isn't discovered here -- the
backend's ``dfu-util -w`` simply waits for it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import FlashError

_SETTLE_S = 2.0          # let the bootloader enumerate after the reset before dfu-util looks


@dataclass(frozen=True)
class Camera:
    port: str
    serial: str | None


def _comports():
    from serial.tools import list_ports
    return list_ports.comports()


def _open_1200(port: str) -> None:
    import serial
    s = serial.Serial(port, 1200)            # the 1200-baud open is the Arduino reset signal
    try:
        s.dtr = True
    finally:
        s.close()


def _ids(spec: str) -> tuple[int, int]:
    vid, pid = spec.split(":")
    return int(vid, 16), int(pid, 16)


def runtime_ids(raw: dict) -> set[tuple[int, int]]:
    """The app-mode ``(vid, pid)`` pairs that identify this board's *running* firmware."""
    app = raw.get("app")                     # arduino: a base usb + extra app/touch pids
    if app:
        vid = _ids(app["usb"])[0]
        pids = {_ids(app["usb"])[1]} | {int(p, 16) for p in app.get("pids", [])}
        return {(vid, p) for p in pids}
    rt = raw.get("runtime")                   # openmv/imx: a single runtime vid:pid
    return {_ids(rt)} if rt else set()


def discover(raw: dict) -> list[Camera]:
    """Running cameras of this board found on the serial ports (port + USB serial number)."""
    ids = runtime_ids(raw)
    return [Camera(p.device, p.serial_number)
            for p in _comports() if (p.vid, p.pid) in ids]


def reset(raw: dict, cam: Camera, *, mpremote: list[str]) -> None:
    """Reset a running camera into its bootloader, then settle while it re-enumerates."""
    from . import runner
    if raw.get("app"):                        # arduino: 1200-baud touch
        _open_1200(cam.port)
    else:                                     # openmv protocol: machine.bootloader()
        runner.run([*mpremote, "connect", cam.port, "bootloader"])
    time.sleep(_SETTLE_S)


def select(raw: dict, serial: str | None) -> Camera | None:
    """The one running camera of this board to flash, or ``None`` if none is running (it's
    already in the bootloader, or not attached yet). Raises if several match and no
    ``--serial`` disambiguates."""
    cams = discover(raw)
    if serial is not None:
        cams = [c for c in cams if c.serial == serial]
    if not cams:
        return None                           # already in the bootloader / not attached yet
    if len(cams) > 1:
        raise FlashError("multiple cameras attached (%s) -- pick one with --serial <sn>"
                         % ", ".join(repr(c.serial) for c in cams))
    return cams[0]
