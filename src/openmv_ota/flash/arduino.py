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
    ``serial`` pins it to one specific board (``-S``) when several are in DFU at once."""
    target = (addr + ":leave") if leave else addr
    argv = [dfu_util, "-w", "-d", ",%s" % usb]
    if serial:
        argv += ["-S", serial]
    return argv + ["-a", str(alt), "-s", target, "-D", str(file)]


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
