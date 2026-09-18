"""App version: a ``MAJOR.MINOR.PATCH[.BUILD]`` string <-> the trailer's uint32
``payload_version``.

The app declares its version as a human semver in ``app/settings.json``; the build
encodes it into the monotonic ``payload_version`` the device uses for anti-rollback.
The encoding matches ``min_platform_version``: ``(major << 24) | (minor << 16) |
(patch << 8) | build``, so the natural numeric order of versions is preserved.

**The fourth component is a build number**, and it exists because three components are
not many. A plain semver reaches 2**24 distinct versions; that is a comfortable ceiling
for one product line and a low one for a fleet operator publishing a build per customer,
where the version space is shared by everything a device might ever be moved onto. The
build byte multiplies the space by 256 without touching the human-facing number: ship
``1.4.2`` and rebuild it as ``1.4.2.1``, ``1.4.2.2`` and so on, and ``1.4.3`` still sorts
above every one of them. Omit it and it is zero, which is exactly what a three-component
version has always encoded -- so nothing about an existing version changes.
"""

from __future__ import annotations

import re

from .errors import OtaError

_VERSION = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?\s*$")


def parse_semver(version: str) -> tuple[int, int, int, int]:
    """Validate ``MAJOR.MINOR.PATCH`` or ``MAJOR.MINOR.PATCH.BUILD`` (each 0-255).

    Returns four components; ``build`` is 0 when the string has three. Raises
    ``OtaError`` otherwise."""
    match = _VERSION.match(version)
    if not match:
        raise OtaError("invalid app version %r: expected MAJOR.MINOR.PATCH[.BUILD]" % version)
    major, minor, patch = (int(part) for part in match.groups()[:3])
    build = int(match.group(4) or 0)
    for part in (major, minor, patch, build):
        if part > 255:
            raise OtaError("app version component %d exceeds 255 in %r" % (part, version))
    return major, minor, patch, build


def encode_app_version(version: str) -> int:
    """Encode a version string into the uint32 ``payload_version``."""
    major, minor, patch, build = parse_semver(version)
    return (major << 24) | (minor << 16) | (patch << 8) | build


def decode_app_version(code: int) -> str:
    """Decode a uint32 ``payload_version`` / ``min_platform_version`` back to a
    ``MAJOR.MINOR.PATCH`` string (with a trailing ``.build`` only if non-zero), for
    display by ``build inspect``."""
    major, minor, patch, build = (
        (code >> 24) & 0xFF, (code >> 16) & 0xFF, (code >> 8) & 0xFF, code & 0xFF)
    base = "%d.%d.%d" % (major, minor, patch)
    return base + (".%d" % build if build else "")
