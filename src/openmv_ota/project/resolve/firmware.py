"""Resolve the OpenMV firmware version from the checkout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import ProjectError
from .macros import parse_defines

_HEADER = "protocol/omv_protocol.h"
_MACROS = [
    "OMV_FIRMWARE_VERSION_MAJOR",
    "OMV_FIRMWARE_VERSION_MINOR",
    "OMV_FIRMWARE_VERSION_PATCH",
]


@dataclass(frozen=True)
class FirmwareVersion:
    major: int
    minor: int
    patch: int

    @property
    def string(self) -> str:
        return "%d.%d.%d" % (self.major, self.minor, self.patch)

    @property
    def code(self) -> int:
        """Packed ``(major<<24)|(minor<<16)|(patch<<8)`` (the trailer encoding)."""
        return (self.major << 24) | (self.minor << 16) | (self.patch << 8)


def resolve_firmware_version(repo: Path) -> FirmwareVersion:
    header = repo / _HEADER
    try:
        text = header.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ProjectError("cannot read %s: %s" % (_HEADER, e)) from None

    defines = parse_defines(text, _MACROS)
    missing = [m for m in _MACROS if m not in defines]
    if missing:
        raise ProjectError(
            "could not find %s in %s" % (", ".join(missing), _HEADER)
        )
    try:
        return FirmwareVersion(
            int(defines["OMV_FIRMWARE_VERSION_MAJOR"], 0),
            int(defines["OMV_FIRMWARE_VERSION_MINOR"], 0),
            int(defines["OMV_FIRMWARE_VERSION_PATCH"], 0),
        )
    except ValueError:
        raise ProjectError("non-integer OMV_FIRMWARE_VERSION_* in %s" % _HEADER) from None
