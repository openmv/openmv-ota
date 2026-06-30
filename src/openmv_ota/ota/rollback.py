"""The anti-rollback floor — an append-only log of confirmed versions in a slot's rollback
sector (one flash erase block).

A device must never be downgraded to an older *signed* release (a replay attack: the
signature is genuine, just stale). So ``confirm()`` appends the running ``payload_version``
to the **BACK** slot's rollback sector and boot.py rejects any FRONT image whose version is
below the highest recorded one.

The log is append-only: each entry is written over erased ``0xFF`` (a 1->0 program, no
erase), so a power loss mid-append leaves a torn entry that simply decodes as invalid and is
ignored — there is never a window where the floor reads blank. An entry is a uint32 version
plus its ones-complement; the two disagree for a blank (``0xFFFF...``) or torn slot, so only
a fully-written entry counts. A 4 KiB sector holds 512 entries (one per *confirmed* update);
when it fills the floor simply freezes at its max — the floor only ever needs to rise, and we
can't erase a single sector to recycle the space, so freezing is both fine and unavoidable.
"""

from __future__ import annotations

import struct

ENTRY_SIZE = 8                       # u32 version || u32 ~version (validity check)
_BLANK = b"\xff" * ENTRY_SIZE
_MASK = 0xFFFFFFFF


def encode_entry(version: int) -> bytes:
    """One log entry recording ``version`` (a uint32 payload_version)."""
    return struct.pack("<II", version & _MASK, (version & _MASK) ^ _MASK)


def _entry_version(entry) -> int | None:
    """The version in an entry, or None if it's blank/torn (the two halves disagree)."""
    version, check = struct.unpack("<II", entry)
    return version if (version ^ _MASK) == check else None


def floor_of(sector) -> int:
    """The anti-rollback floor recorded in a sector: the highest valid entry version (0 if
    none — a blank/factory sector imposes no floor)."""
    floor = 0
    for i in range(0, len(sector) - ENTRY_SIZE + 1, ENTRY_SIZE):
        version = _entry_version(bytes(sector[i:i + ENTRY_SIZE]))
        if version is not None and version > floor:
            floor = version
    return floor


def append_offset(sector) -> int | None:
    """Offset of the first blank entry slot to append into, or None if the sector is full
    (every slot written); the caller then leaves the floor frozen at its max."""
    for i in range(0, len(sector) - ENTRY_SIZE + 1, ENTRY_SIZE):
        if bytes(sector[i:i + ENTRY_SIZE]) == _BLANK:
            return i
    return None
