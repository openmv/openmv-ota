"""The slot **status sector** — its per-slot markers, written as a slot moves through the
trial-boot lifecycle.

A slot's status sector (one flash erase block) holds 16-byte markers at fixed offsets.
Each is a high-entropy sentinel written **over** the erased ``0xFF`` (a 1->0 transition, so
no erase is needed); a marker counts as set only on an exact 16-byte match, so a
torn/partial write — or an unwritten slot — reads as "not set" (the safe default).

    offset 0   pending    the updater wrote it after staging a new image
    offset 16  tried      boot.py wrote it on the first (one-shot) trial boot
    offset 32  confirmed  the app wrote it after its self-test passed
    offset 48  repr       how the updater installed the image (REPR_FULL / REPR_DELTA)
    offset 64  counter    the install counter (u32 || ~u32) — which slot is newer
    offset 80  attempts   one 16-byte marker per trial boot consumed

``pending``/``tried``/``confirmed`` are the trial state machine boot.py acts on; ``repr``
is the provenance the updater stamps beside ``pending`` so a later boot's ``status()`` can
report whether a full image or a delta was applied (an unwritten slot — a factory image —
reads as neither). 16 bytes = 128 bits (overwhelming collision resistance) and exactly one
AE3-MRAM write unit, so each marker is a single atomic write. The values are SHA-256 of
labelled strings — reproducible and documented, not arbitrary magic. boot.py, the updater,
and ``build factory-romfs`` all share these definitions so they can't drift.

The two v2 fields past the markers exist because A/B needs an *order* and a trial needs a
*budget*, and neither can be a value that gets rewritten: everything here is written over
erased ``0xFF`` and there is no erase available after the slot's one erase pass. So the
counter is written once (with its ones-complement, so a torn write is detectable rather
than believed) and the attempt budget is an append region — one 16-byte marker consumed per
boot, exactly like the rollback log's append-only entries.
"""

from __future__ import annotations

import hashlib
import struct

MARKER_SIZE = 16
PENDING_OFFSET = 0
TRIED_OFFSET = 16
CONFIRMED_OFFSET = 32
REPR_OFFSET = 48

# --- v2 fields (mirror of boot.py's _COUNTER_OFF / _ATTEMPTS_OFF) ------------
COUNTER_OFFSET = 64
COUNTER_SIZE = 8                     # u32 value || u32 ~value
# ONE 16-BYTE MARKER PER ATTEMPT. 16 is the portable flash write unit here -- exactly one AE3
# MRAM write unit, and what every marker above already uses. A one-byte write is NOT portable:
# the N6's XSPI runs octal DTR (two bytes per clock) and a single-byte program hard faults in
# the driver -- silently, on the first boot of every trial. Found on hardware.
ATTEMPT_UNIT = 16
ATTEMPTS_OFFSET = 80                 # 16-byte aligned, clear of the counter at 64..71
ATTEMPTS_MAX = 64                    # a slot needing 64 boots to come up is not coming back
_MASK = 0xFFFFFFFF


def _marker(label: bytes) -> bytes:
    return hashlib.sha256(b"openmv-ota.status." + label).digest()[:MARKER_SIZE]


PENDING = _marker(b"pending")
TRIED = _marker(b"tried")
CONFIRMED = _marker(b"confirmed")
REPR_FULL = _marker(b"repr.full")
REPR_DELTA = _marker(b"repr.ocdl")
# The value written into the attempt region, one marker per trial boot. This module already
# describes WHERE that region is (ATTEMPTS_OFFSET/ATTEMPT_UNIT/ATTEMPTS_MAX); carrying the value
# too is what lets anything off-device build a spent-trial sector without hand-rolling bytes.
# It is not decoration: the QEMU suite did hand-roll them, against the pre-16-byte layout, and
# the resulting sector still had attempts left -- so its "a spent trial is rejected" case
# silently asserted nothing until CI caught it.
ATTEMPT = _marker(b"attempt")


def encode_counter(value: int) -> bytes:
    """The install-counter field recording ``value``: the u32 plus its ones-complement.

    Same self-validating shape as a rollback entry, and for the same reason — the field is
    written into flash that cannot be erased again, so a power loss mid-write has to read as
    *unknown* rather than as some other number. ``boot.install_counter`` rejects any pair whose
    halves disagree, and a slot with no readable counter simply sorts last."""
    return struct.pack("<II", value & _MASK, (value & _MASK) ^ _MASK)


def build_status_sector(block: int, *, pending: bool, tried: bool, confirmed: bool,
                        counter: int | None = None) -> bytes:
    """A ``block``-sized status sector with the requested markers set (rest ``0xFF``).

    Under v2 both slots are real, updatable images and share one shape: an installed slot is
    ``pending`` (a trial) and becomes ``confirmed`` when the app keeps it. A provisioned board
    ships both slots already ``confirmed`` — they have nothing to prove — and ``counter`` orders
    them, so which one boots is decided by the same rule that decides it after every later
    update rather than by a factory-only special case."""
    sector = bytearray(b"\xff" * block)
    if pending:
        sector[PENDING_OFFSET:PENDING_OFFSET + MARKER_SIZE] = PENDING
    if tried:
        sector[TRIED_OFFSET:TRIED_OFFSET + MARKER_SIZE] = TRIED
    if confirmed:
        sector[CONFIRMED_OFFSET:CONFIRMED_OFFSET + MARKER_SIZE] = CONFIRMED
    if counter is not None:
        sector[COUNTER_OFFSET:COUNTER_OFFSET + COUNTER_SIZE] = encode_counter(counter)
    return bytes(sector)
