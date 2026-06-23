"""App version: a ``MAJOR.MINOR.PATCH`` semver string <-> the trailer's uint32
``payload_version``.

The app declares its version as a human semver in ``app/settings.json``; the build
encodes it into the monotonic ``payload_version`` the device uses for anti-rollback.
The encoding matches ``min_platform_version``: ``(major << 24) | (minor << 16) |
(patch << 8) | build`` with the low ``build`` byte reserved (0 for a plain semver),
so the natural numeric order of versions is preserved.
"""

from __future__ import annotations

import re

from .errors import OtaError

_SEMVER = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)\s*$")


def parse_semver(version: str) -> tuple[int, int, int]:
    """Validate ``MAJOR.MINOR.PATCH`` (each 0-255). Raises ``OtaError`` otherwise."""
    match = _SEMVER.match(version)
    if not match:
        raise OtaError("invalid app version %r: expected MAJOR.MINOR.PATCH" % version)
    major, minor, patch = (int(part) for part in match.groups())
    for part in (major, minor, patch):
        if part > 255:
            raise OtaError("app version component %d exceeds 255 in %r" % (part, version))
    return major, minor, patch


def encode_app_version(version: str) -> int:
    """Encode a semver into the uint32 ``payload_version``."""
    major, minor, patch = parse_semver(version)
    return (major << 24) | (minor << 16) | (patch << 8)
