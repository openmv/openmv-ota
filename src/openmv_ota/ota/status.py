"""The slot **status sector** — the on-device trial-boot state machine's markers.

A slot's status sector (one flash erase block) holds three 16-byte markers at
fixed offsets. Each is a high-entropy sentinel written **over** the erased ``0xFF``
(a 1->0 transition, so no erase is needed); a marker counts as set only on an exact
16-byte match, so a torn/partial write reads as "not set" (the safe default).

    offset 0   pending    the updater wrote it after staging a new image
    offset 16  tried      boot.py wrote it on the first (one-shot) trial boot
    offset 32  confirmed  the app wrote it after its self-test passed

16 bytes = 128 bits (overwhelming collision resistance) and exactly one AE3-MRAM
write unit, so each marker is a single atomic write. The values are SHA-256 of
labelled strings — reproducible and documented, not arbitrary magic. boot.py, the
updater, and ``build factory-romfs`` all share these definitions so they can't drift.
"""

from __future__ import annotations

import hashlib

MARKER_SIZE = 16
PENDING_OFFSET = 0
TRIED_OFFSET = 16
CONFIRMED_OFFSET = 32


def _marker(label: bytes) -> bytes:
    return hashlib.sha256(b"openmv-ota.status." + label).digest()[:MARKER_SIZE]


PENDING = _marker(b"pending")
TRIED = _marker(b"tried")
CONFIRMED = _marker(b"confirmed")


def build_status_sector(block: int, *, pending: bool, tried: bool, confirmed: bool) -> bytes:
    """A ``block``-sized status sector with the requested markers set (rest ``0xFF``).

    The two factory shapes:

    - **BACK** (golden / factory state): ``confirmed`` only.
    - **FRONT** (initial ship): ``pending + tried + confirmed`` — the
      "post-OTA-confirmed" shape, because boot.py's FRONT branch rejects the
      ``confirmed``-only shape (that's BACK-only).
    """
    sector = bytearray(b"\xff" * block)
    if pending:
        sector[PENDING_OFFSET:PENDING_OFFSET + MARKER_SIZE] = PENDING
    if tried:
        sector[TRIED_OFFSET:TRIED_OFFSET + MARKER_SIZE] = TRIED
    if confirmed:
        sector[CONFIRMED_OFFSET:CONFIRMED_OFFSET + MARKER_SIZE] = CONFIRMED
    return bytes(sector)
