"""OTA slot geometry, derived from a partition's flash erase block.

A ROMFS partition is split 50/50 into a **FRONT** (mutable runtime) slot and a
**BACK** (immutable golden) slot. Each slot holds the ROMFS body plus two
control sectors -- a **status** sector and a **trailer** sector -- each one flash
erase block so they can be erased/rewritten independently of the body.

Everything keys off the erase block, but floored to ``MIN_OTA_BLOCK``: a
byte-writable backing store like AE3's MRAM reports a tiny 16-byte "sector", and
sizing the trailer to that would leave no room to grow the signed metadata later
without reshaping the layout and breaking already-deployed devices. Reserving a
full 4 KiB block instead costs nothing on a multi-megabyte partition.

A partition is **OTA-capable** only if a slot has room for a body after its two
control sectors -- which excludes boards whose ROMFS is a single large internal
flash sector (e.g. OpenMV2/3/4), where the math itself proves OTA is impossible.
"""

from __future__ import annotations

MIN_OTA_BLOCK = 4096  # trailer/status reserve at least one 4 KiB block


def ota_block(erase_size: int) -> int:
    """The block the OTA layout aligns to: the flash erase block, floored to 4 KiB."""
    return max(int(erase_size), MIN_OTA_BLOCK)


def front_size(partition_size: int, erase_size: int) -> int:
    """FRONT slot size: half the partition, aligned **down** to a block so FRONT can
    be erased without disturbing the golden BACK half."""
    blk = ota_block(erase_size)
    return (int(partition_size) // 2) & ~(blk - 1)


def slot_overhead(erase_size: int) -> int:
    """Per-slot control overhead: a status sector + a trailer sector (one block each)."""
    return 2 * ota_block(erase_size)


def body_capacity(partition_size: int, erase_size: int) -> int:
    """Usable OTA image bytes in a slot. ``<= 0`` means the partition can't host OTA."""
    return front_size(partition_size, erase_size) - slot_overhead(erase_size)


def is_ota_capable(partition_size: int, erase_size: int) -> bool:
    """Whether a partition can host an OTA image (a slot has a non-empty body)."""
    return body_capacity(partition_size, erase_size) > 0
