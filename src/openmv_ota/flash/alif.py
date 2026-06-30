"""Flash the AE3 (Alif) bootloader via the openmv-vendored Alif Security Toolkit.

The AE3 talks the Alif SE-UART/ISP protocol over a USB-serial bridge while held in its
maintenance mode. The toolkit ships as Python source in the openmv firmware tree
(``tools/alif/toolkit``); we drive two of its scripts:

* ``updateSystemPackage.py`` -- (re)installs the matching SE firmware. Run first and always:
  it's coupled to the bootloader, and a virgin board *must* be power-cycled afterwards before
  the freshly written SES takes effect -- so the orchestrator tells the operator to
  unplug/replug between the two steps and re-finds the port.
* ``app-write-mram.py`` -- writes the SBL ``bootloader.bin`` to the MRAM base and the padded
  ``firmware_pad.toc`` to the TOC region (``--images "<file> <addr> ..."``). No ``--erase`` --
  the APP mass-erase bricks the board (it's why the IDE keeps that step commented out).

Afterwards the board re-enumerates as the OpenMV DFU bootloader (37c5:96e3); ``flash firmware``
writes the application over DFU.

The two board revisions differ only by their bridge and Part#: SBL (FTDI ``0403:6015`` /
``AE302F80F55D5AE``) and SBL2 (CH340 ``1a86:55d3`` / ``AE302F80F55D5LE``); both are rev B4.
The scripts resolve their config DBs relative to their own location, so they run from any cwd;
they need only pyserial.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import FlashError


@dataclass(frozen=True)
class AlifStep:
    label: str
    argv: list[str]


@dataclass(frozen=True)
class SeUart:
    port: str
    cfg_part: str
    name: str


def _ids(spec: str) -> tuple[int, int]:
    vid, pid = spec.split(":")
    return int(vid, 16), int(pid, 16)


def find_se_uart(variants: list[dict], comports) -> SeUart:
    """The one AE3 SE-UART bridge among the serial ports, resolved to its ``cfg_part``. Raises
    a clear ``FlashError`` if none (board not in maintenance mode) or several are attached."""
    by_ids = {_ids(v["bridge"]): v for v in variants}
    found = [SeUart(p.device, by_ids[(p.vid, p.pid)]["cfg_part"], by_ids[(p.vid, p.pid)]["name"])
             for p in comports if (p.vid, p.pid) in by_ids]
    if not found:
        raise FlashError(
            "no AE3 in SE-UART maintenance mode found -- enter maintenance mode and replug, "
            "then retry (looked for %s)" % ", ".join(v["bridge"] for v in variants))
    if len(found) > 1:
        raise FlashError("multiple AE3 SE-UART devices attached (%s) -- connect just one"
                         % ", ".join(repr(s.port) for s in found))
    return found[0]


def images_arg(images: list[dict], files: dict) -> str:
    """The ``app-write-mram --images`` string: ``<file> <addr> <file> <addr>``."""
    return " ".join("%s %s" % (files[i["file"]], i["addr"]) for i in images)


def _script(python3: str, toolkit: str, name: str, se: SeUart, rev: str, *extra: str) -> list[str]:
    return [python3, str(Path(toolkit) / name), "--port", se.port,
            "--cfg-part", se.cfg_part, "--cfg-rev", rev, *extra]


def update_system_package_argv(python3: str, toolkit: str, se: SeUart, rev: str) -> list[str]:
    return _script(python3, toolkit, "updateSystemPackage.py", se, rev)


def write_bootloader_argv(python3: str, toolkit: str, se: SeUart, rev: str,
                          images: str) -> list[str]:
    return _script(python3, toolkit, "app-write-mram.py", se, rev, "--pad", "--images", images)
