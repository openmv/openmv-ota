"""The Arduino (Portenta H7 / Giga / Nicla Vision) backend: address-based ``dfu-util``.

These boards run the Arduino MCUboot DFU bootloader, which addresses flash by absolute
**address** (``-a <alt> -s 0xADDR``) rather than by alt alone, and leaves DFU via
``-s 0xADDR:leave`` (not ``--reset``). The same ``dfu-util -w -d ,<vid:pid>`` wrapper as the
OpenMV boards still applies. Writes erase-on-write, so no separate erase pass is needed.

A full provision (``flash factory``) also writes the shared **CYW4343** wifi/bt firmware
blobs to QSPI -- prebuilt copies bundled in the package, so the user never supplies them.

To flash, the board must be in the DFU bootloader. If it's in app mode we **touch-to-reset**
it: open its serial port at 1200 baud, which the bootloader detects and reboots into DFU
(then ``dfu-util -w`` waits for it). If no app-mode port is found the board is assumed to be
in the bootloader already (the user double-tapped reset), and ``-w`` waits regardless.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

_TOUCH_SETTLE_S = 2.0            # let the board re-enumerate in DFU after the 1200-baud touch


@dataclass(frozen=True)
class ArduinoStep:
    label: str
    argv: list[str]


def program_argv(dfu_util: str, usb: str, alt: int, addr: str, file: Path, *,
                 leave: bool = False) -> list[str]:
    """Argv to write ``file`` to ``addr`` on alt ``alt``; ``leave`` exits DFU after the write."""
    target = (addr + ":leave") if leave else addr
    return [dfu_util, "-w", "-d", ",%s" % usb, "-a", str(alt), "-s", target, "-D", str(file)]


def plan(op: str, raw: dict, dfu_util: str, files: dict) -> list[ArduinoStep]:
    """The ordered writes for an Arduino ``op``. ``files`` holds the resolved paths:
    ``firmware``/``romfs`` as the op needs, plus ``wifi`` (a list) for a factory flash."""
    usb, fw, ro = raw["usb"], raw["firmware"], raw["romfs"]
    writes = []                                       # (alt, addr, path, label)
    if op == "factory":                               # full provision: wifi, firmware, romfs
        for entry, path in zip(raw["wifi"], files["wifi"]):
            writes.append((entry["alt"], entry["addr"], path, "wifi %s" % path.name))
        writes.append((fw["alt"], fw["addr"], files["firmware"], "firmware"))
        writes.append((ro["alt"], ro["addr"], files["romfs"], "romfs"))
    elif op == "firmware":
        writes.append((fw["alt"], fw["addr"], files["firmware"], "firmware"))
    else:                                             # romfs
        writes.append((ro["alt"], ro["addr"], files["romfs"], "romfs"))

    steps = []
    last = len(writes) - 1
    for i, (alt, addr, path, label) in enumerate(writes):
        leave = i == last                             # only the final write leaves DFU
        steps.append(ArduinoStep("%s -> %s%s" % (label, addr, ":leave" if leave else ""),
                                 program_argv(dfu_util, usb, alt, addr, path, leave=leave)))
    return steps


def _comports():
    from serial.tools import list_ports
    return list_ports.comports()


def _open_1200(port: str) -> None:
    import serial
    s = serial.Serial(port, 1200)                     # the 1200-baud open is the reset signal
    try:
        s.dtr = True
    finally:
        s.close()


def touch_to_reset(raw: dict) -> str | None:
    """If the board is in app mode, 1200-baud touch its serial port to reboot it into DFU.
    Returns the port touched, or ``None`` if it's not in app mode (already in the bootloader)."""
    app = raw.get("app")
    if not app:
        return None
    vid = int(app["usb"].split(":")[0], 16)
    pids = {int(p, 16) for p in app.get("pids", [])}
    for p in _comports():
        if p.vid == vid and p.pid in pids:
            _open_1200(p.device)
            time.sleep(_TOUCH_SETTLE_S)
            return p.device
    return None
