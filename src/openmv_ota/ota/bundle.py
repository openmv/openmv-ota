"""The OTA release bundle: a zip of the body image, the signed trailer, and a
plaintext manifest, so a release moves (flash / upload / inspect) as one file.

Entries (generic names; the zip itself is named per-board):

    romfs.img      the ROMFS body (mounted at /rom on the device)
    trailer.bin    the signed trailer (authenticated; the slot's last erase block)
    manifest.json  a copy of /rom/system.json, for codec-free indexing by tools

The device never receives the zip — it can't hold the body in RAM to unzip — so a
server unbundles and streams the body + trailer separately. The bundle is purely a
host/server-side convenience.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from .errors import OtaError

ROMFS = "romfs.img"
TRAILER = "trailer.bin"
MANIFEST = "manifest.json"


def write_bundle(path: Path, body: bytes, trailer_bytes: bytes, manifest: dict) -> None:
    """Write a ``<board>.zip`` bundle (deterministic JSON manifest)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        z.writestr(ROMFS, body)
        z.writestr(TRAILER, trailer_bytes)


def is_bundle(path: Path) -> bool:
    """Whether ``path`` is a zip (i.e. a bundle, not a loose image/trailer)."""
    return zipfile.is_zipfile(path)


def read_bundle(path: Path) -> tuple[bytes, bytes, dict]:
    """Return ``(body, trailer_bytes, manifest)`` from a bundle. Raises ``OtaError``
    if it isn't a well-formed OTA bundle."""
    try:
        with zipfile.ZipFile(path) as z:
            return z.read(ROMFS), z.read(TRAILER), json.loads(z.read(MANIFEST))
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError) as e:
        raise OtaError("not an OTA bundle: %s" % e) from None
