"""The anti-rollback floor — appended entries in the tail of a slot's STATUS sector
(``openmv_ota.ota.status.floor_offset``).

A device must never be downgraded to an older *signed* release (a replay attack: the
signature is genuine, just stale). The floor must *rise* at ``confirm()``, but flash only
programs bits 1->0 — a stored value cannot be overwritten with a bigger one without an
erase. So the floor is held as appended entries: raising it programs a fresh entry into
blank bytes, and the current floor is the highest valid entry. An entry is a uint32
version plus its ones-complement; the halves disagree for a blank or torn entry, so a
power loss mid-append is simply ignored. In practice a sector cycle holds one or two
entries — the floor the installer carried in, plus one raise at the first ``confirm()`` —
before the slot's next install erases and re-seeds it.

The helpers here scan whatever buffer they are given; callers pass the status sector's
floor region (``sector[floor_offset(stride):]``).
"""

from __future__ import annotations

import struct

ENTRY_SIZE = 8                       # u32 version || u32 ~version (validity check)
_BLANK = b"\xff" * ENTRY_SIZE
# Entries are read AND appended at the control stride (16 default; 32 on H7-classic
# internal flash, where each append must own a whole ECC word). The 8 payload bytes
# stay identical; the stride only spaces them.
_MASK = 0xFFFFFFFF


def encode_entry(version: int) -> bytes:
    """One log entry recording ``version`` (a uint32 payload_version)."""
    return struct.pack("<II", version & _MASK, (version & _MASK) ^ _MASK)


def _entry_version(entry) -> int | None:
    """The version in an entry, or None if it's blank/torn (the two halves disagree)."""
    version, check = struct.unpack("<II", entry)
    return version if (version ^ _MASK) == check else None


def floor_of(sector, stride: int = ENTRY_SIZE) -> int:
    """The anti-rollback floor recorded in a sector: the highest valid entry version (0 if
    none — a blank/factory sector imposes no floor)."""
    floor = 0
    for i in range(0, len(sector) - ENTRY_SIZE + 1, stride):
        version = _entry_version(bytes(sector[i:i + ENTRY_SIZE]))
        if version is not None and version > floor:
            floor = version
    return floor


def append_offset(sector, stride: int = ENTRY_SIZE) -> int | None:
    """Offset of the first blank entry slot to append into, or None if the sector is full
    (every slot written); the caller then leaves the floor frozen at its max."""
    for i in range(0, len(sector) - ENTRY_SIZE + 1, stride):
        if bytes(sector[i:i + ENTRY_SIZE]) == _BLANK:
            return i
    return None
