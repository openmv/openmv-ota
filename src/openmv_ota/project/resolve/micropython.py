"""Resolve the MicroPython version and the ``.mpy`` ABI from the submodule."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import ProjectError
from .macros import parse_defines

MICROPYTHON_SUBPATH = "lib/micropython"
_MPCONFIG = "py/mpconfig.h"
_PERSISTENT = "py/persistentcode.h"

_VERSION_MACROS = [
    "MICROPY_VERSION_MAJOR",
    "MICROPY_VERSION_MINOR",
    "MICROPY_VERSION_MICRO",
    "MICROPY_VERSION_PRERELEASE",
]


@dataclass(frozen=True)
class MicroPythonInfo:
    version: str
    prerelease: bool
    mpy_abi_version: int
    mpy_sub_version: int


def resolve_micropython(repo: Path) -> MicroPythonInfo:
    mp = repo / MICROPYTHON_SUBPATH
    mpconfig = mp / _MPCONFIG
    persistent = mp / _PERSISTENT
    if not mpconfig.exists():
        raise ProjectError(
            "micropython submodule not initialized (missing %s/%s); run "
            "git submodule update --init" % (MICROPYTHON_SUBPATH, _MPCONFIG)
        )

    cfg = parse_defines(mpconfig.read_text(encoding="utf-8", errors="replace"), _VERSION_MACROS)
    missing = [m for m in _VERSION_MACROS if m not in cfg]
    if missing:
        raise ProjectError("could not find %s in %s" % (", ".join(missing), _MPCONFIG))

    pc = parse_defines(
        persistent.read_text(encoding="utf-8", errors="replace"),
        ["MPY_VERSION", "MPY_SUB_VERSION"],
    )
    if "MPY_VERSION" not in pc or "MPY_SUB_VERSION" not in pc:
        raise ProjectError("could not find MPY_VERSION/MPY_SUB_VERSION in %s" % _PERSISTENT)

    try:
        version = "%d.%d.%d" % (
            int(cfg["MICROPY_VERSION_MAJOR"], 0),
            int(cfg["MICROPY_VERSION_MINOR"], 0),
            int(cfg["MICROPY_VERSION_MICRO"], 0),
        )
        return MicroPythonInfo(
            version=version,
            prerelease=int(cfg["MICROPY_VERSION_PRERELEASE"], 0) != 0,
            mpy_abi_version=int(pc["MPY_VERSION"], 0),
            mpy_sub_version=int(pc["MPY_SUB_VERSION"], 0),
        )
    except ValueError:
        raise ProjectError("non-integer MicroPython version constant") from None
