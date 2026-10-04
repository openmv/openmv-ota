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

A board already in its bootloader isn't a serial port, so it isn't discovered here. Nothing here
waits for the bootloader to appear either: the flash backend polls for the DFU device itself
(``flash._await_dfu``), which is both faster than a fixed settle and bounded.
"""

from __future__ import annotations

import sys

from dataclasses import dataclass

from .errors import FlashError


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


def reset(raw: dict, cam: Camera, *, mpremote: list[str]) -> str | None:
    """Reset a running camera into its bootloader (the caller waits for it to enumerate).

    Returns mpremote's captured output when it exited non-zero, else ``None`` -- NOT an error by
    itself (see below): the caller shows it only if the bootloader then never appears."""
    from . import runner
    if raw.get("app"):                        # arduino: 1200-baud touch
        _open_1200(cam.port)
        return None
    # openmv protocol: exec machine.bootloader() rather than mpremote's `bootloader` subcommand: the
    # subcommand's entry method works on stm32 (N6) but silently no-ops on the alif (AE3), which
    # drops into DFU only via a direct machine.bootloader() call. That call tears down the USB-CDC
    # mid-exec, so mpremote exits non-zero with ~40 lines of traceback (OSError EIO on the now-gone
    # port) even though the reset landed. A first-time user reads that as a failure, so its output
    # is captured and one plain line printed instead; whether the reset WORKED is decided by the
    # caller's bounded wait for the bootloader, which shows this output if it never appears.
    print("camera rebooting into its bootloader...", file=sys.stderr)
    rc, out = runner.run_quiet([*mpremote, "connect", cam.port, "exec",
                                "import machine; machine.bootloader()"])
    if rc == 0:
        return None
    return out.strip() or "mpremote exited %d" % rc


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
