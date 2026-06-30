"""OTA slot geometry, derived from a partition's flash erase block.

A ROMFS partition is split 50/50 into a **FRONT** (mutable runtime) slot and a
**BACK** (immutable golden) slot. Each slot holds the ROMFS body plus four
control sectors at the **end** of the slot, each one flash erase block so they can
be erased/rewritten independently of the body. The three used sectors are contiguous
at the very end; ``spare`` is the lone buffer between them and the body. Counting back
from the last block::

    slot_size - 1*block   trailer    the signed trust trailer
    slot_size - 2*block   status     the trial-boot state machine markers
    slot_size - 3*block   rollback   the monotonic anti-rollback floor
    slot_size - 4*block   spare      reserved for future metadata

The ``rollback`` sector holds a fixed-size append-only log of confirmed versions (one
block, ~500 entries); ``confirm()`` appends the running version (a 1->0 program, no erase)
and boot.py takes the max as the anti-rollback floor, so a device can't be downgraded to
an older *signed* release. When the log fills the floor freezes at its max -- still
protective. The **BACK** slot's rollback sector is the authoritative one (FRONT is erased
on every install); the FRONT copy is reserved for symmetry. ``spare`` is held back so the
next metadata need doesn't force a layout change that would re-base every fielded device.

Everything keys off the erase block, but floored to ``MIN_OTA_BLOCK``: a
byte-writable backing store like AE3's MRAM reports a tiny 16-byte "sector", and
sizing the trailer to that would leave no room to grow the signed metadata later
without reshaping the layout and breaking already-deployed devices. Reserving a
full 4 KiB block instead costs nothing on a multi-megabyte partition.

A partition is **OTA-capable** only if a slot has room for a body after its
control sectors -- which excludes boards whose ROMFS is a single large internal
flash sector (e.g. OpenMV2/3/4), where the math itself proves OTA is impossible.
"""

from __future__ import annotations

MIN_OTA_BLOCK = 4096  # each control sector reserves at least one 4 KiB block
CONTROL_SECTORS = 4   # spare, rollback, status, trailer (in ascending offset order)


def ota_block(erase_size: int) -> int:
    """The block the OTA layout aligns to: the flash erase block, floored to 4 KiB."""
    return max(int(erase_size), MIN_OTA_BLOCK)


def front_size(partition_size: int, erase_size: int) -> int:
    """FRONT slot size: half the partition, aligned **down** to a block so FRONT can
    be erased without disturbing the golden BACK half."""
    blk = ota_block(erase_size)
    return (int(partition_size) // 2) & ~(blk - 1)


def slot_overhead(erase_size: int) -> int:
    """Per-slot control overhead: the trailer/status/rollback/spare sectors (one block each)."""
    return CONTROL_SECTORS * ota_block(erase_size)


def trailer_offset(slot_size: int, erase_size: int) -> int:
    """Offset of the trailer sector within a slot (the last block)."""
    return slot_size - ota_block(erase_size)


def status_offset(slot_size: int, erase_size: int) -> int:
    """Offset of the status sector within a slot."""
    return slot_size - 2 * ota_block(erase_size)


def rollback_offset(slot_size: int, erase_size: int) -> int:
    """Offset of the anti-rollback (version-floor) sector within a slot."""
    return slot_size - 3 * ota_block(erase_size)


def body_capacity(partition_size: int, erase_size: int) -> int:
    """Usable OTA image bytes in a slot. ``<= 0`` means the partition can't host OTA."""
    return front_size(partition_size, erase_size) - slot_overhead(erase_size)


def is_ota_capable(partition_size: int, erase_size: int) -> bool:
    """Whether a partition can host an OTA image (a slot has a non-empty body)."""
    return body_capacity(partition_size, erase_size) > 0
