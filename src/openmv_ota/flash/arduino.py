"""The Arduino (Portenta H7 / Giga / Nicla Vision) backend: address-based ``dfu-util``.

These boards run the Arduino MCUboot DFU bootloader, which addresses flash by absolute
**address** (``-a <alt> -s 0xADDR``) rather than by alt alone, and leaves DFU via
``-s 0xADDR:leave`` (not ``--reset``). The same ``dfu-util -w -d ,<vid:pid>`` wrapper as the
OpenMV boards still applies. Writes erase-on-write, so no separate erase pass is needed.

A full provision (``flash factory``) also writes the shared **CYW4343** wifi/bt firmware
blobs to QSPI -- prebuilt copies bundled in the package, so the user never supplies them.

Getting the board into its DFU bootloader (the 1200-baud touch) is handled by ``flash.device``
before these writes run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArduinoStep:
    label: str
    argv: list[str]


def program_argv(dfu_util: str, usb: str, alt: int, addr: str, file: Path, *,
                 leave: bool = False, serial: str | None = None) -> list[str]:
    """Argv to write ``file`` to ``addr`` on alt ``alt``; ``leave`` exits DFU after the write.

    NO ``-S`` SERIAL PIN -- ``serial`` is accepted and deliberately ignored. A board's DFU-mode
    serial is NOT its runtime serial, so pinning the runtime one matches nothing and ``-w`` then
    waits forever: the flash appears to hang rather than fail. Measured on a Portenta H7 --
    runtime ``346534563033`` (usb 2341:045b) vs DFU ``0033001F3033510634323437`` (usb 2341:035b),
    entirely different values. That hang cost a 1500 s timeout on the Nicla before it was
    understood. ``flash/dfu.py::download_argv`` dropped the pin for the same reason on the OpenMV
    boards; this is the Arduino path catching up.

    Nothing is lost: the 1200-baud touch puts only THIS board into DFU, and ``-d ,<vid:pid>``
    already selects the bootloader's USB id. The parameter stays so callers (``plan``) need no
    change and a future re-introduction has to think about the mismatch first.
    """
    del serial                       # see above: the DFU serial never matches the runtime one
    target = (addr + ":leave") if leave else addr
    return [dfu_util, "-w", "-d", ",%s" % usb, "-a", str(alt), "-s", target, "-D", str(file)]


def plan(op: str, raw: dict, dfu_util: str, files: dict, serial: str | None = None
         ) -> list[ArduinoStep]:
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
        steps.append(ArduinoStep(
            "%s -> %s%s" % (label, addr, ":leave" if leave else ""),
            program_argv(dfu_util, usb, alt, addr, path, leave=leave, serial=serial)))
    return steps
