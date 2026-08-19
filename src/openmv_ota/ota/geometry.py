"""OTA slot geometry, derived from a partition's flash erase block.

A ROMFS partition is split 50/50 into two slots of equal size. The code names
the halves FRONT and BACK (v1 names that survived the A/B redesign), but neither
is privileged: both are real, signed, updatable images, the newest valid one
boots, and an install writes whichever slot is not running. Each slot holds the
ROMFS body plus four control sectors at the **end** of the slot, each one 4 KiB
``control_block`` -- deliberately *not* the erase block; they are never erased
independently of their slot (see ``control_block`` below). The three used sectors are contiguous
at the very end; ``spare`` is the lone buffer between them and the body. Counting back
from the last block::

    slot_size - 1*block   trailer    the signed trust trailer
    slot_size - 2*block   status     the trial-boot state machine markers
    slot_size - 3*block   rollback   the monotonic anti-rollback floor
    slot_size - 4*block   spare      reserved for future metadata

The ``rollback`` sector holds a fixed-size append-only log of confirmed versions (one
4 KiB block = 512 entries); ``confirm()`` appends the running version (a 1->0 program, no erase)
and boot.py takes the max as the anti-rollback floor, so a device can't be downgraded to
an older *signed* release. When the log fills the floor freezes at its max -- still
protective. No single slot's sector is authoritative: under A/B every
slot is erased in turn, so boot.py reads the floor as the max across both slots and
the installer carries the current floor into each slot it writes. ``spare`` is held back so the
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
    """The block SLOT BOUNDARIES align to: the flash erase block, floored to 4 KiB.

    This one genuinely needs the erase block. A/B only works if erasing one slot cannot disturb
    the other, so the split has to land on an erase boundary."""
    return max(int(erase_size), MIN_OTA_BLOCK)


def control_block(erase_size: int = 0) -> int:
    """The granularity of the CONTROL sectors -- always 4 KiB, never the erase block.

    Sizing these to the erase block conflated two different things. The erase block matters for
    keeping SLOTS separable; it says nothing about how much room the trailer, status, rollback and
    spare records need. And they are never erased independently of their slot: there is exactly one
    erase call in the whole device tree (the installer's slot erase), after which every control
    write is a 1->0 program -- status markers written once each as the trial advances, the rollback
    log append-only, the trailer written at install.

    So on a board with a 128 KiB erase block the old sizing reserved 4 x 128 KiB = 512 KiB of
    control PER SLOT to hold a few hundred bytes of records, which on smaller partitions was the
    difference between OTA-capable and not. ``erase_size`` is accepted and ignored so call sites
    read symmetrically with ``ota_block``."""
    del erase_size
    return MIN_OTA_BLOCK


def front_size(partition_size: int, erase_size: int) -> int:
    """FRONT slot size: half the partition, aligned **down** to a block so FRONT can
    be erased without disturbing the golden BACK half."""
    blk = ota_block(erase_size)
    return (int(partition_size) // 2) & ~(blk - 1)


def slot_overhead(erase_size: int) -> int:
    """Per-slot control overhead: the trailer/status/rollback/spare sectors (one block each)."""
    return CONTROL_SECTORS * control_block(erase_size)


def trailer_offset(slot_size: int, erase_size: int) -> int:
    """Offset of the trailer sector within a slot (the last block)."""
    return slot_size - control_block(erase_size)


def status_offset(slot_size: int, erase_size: int) -> int:
    """Offset of the status sector within a slot."""
    return slot_size - 2 * control_block(erase_size)


def rollback_offset(slot_size: int, erase_size: int) -> int:
    """Offset of the anti-rollback (version-floor) sector within a slot."""
    return slot_size - 3 * control_block(erase_size)


def delta_base_len(slot_size: int, erase_size: int) -> int:
    """How many bytes of a slot a delta may be computed against: the body region, i.e. the
    slot minus its control sectors.

    A delta's base has to be **the same bytes on every device**. The body region is -- it is
    the signed image. The control sectors are NOT: they carry install counters, rollback
    entries, consumed attempt bytes and the CONFIRMED marker, all written per device at
    per-device times. A delta computed over the whole slot can legally copy from that region,
    and then reconstructs differently on each device -- failing the sha256 gate, so the update
    never lands. (Measured, and it applied to v1's golden base too: `confirm()` appended to
    that slot's rollback sector, so it drifted as well.)"""
    return int(slot_size) - slot_overhead(erase_size)


def body_capacity(partition_size: int, erase_size: int) -> int:
    """Usable OTA image bytes in a slot. ``<= 0`` means the partition can't host OTA."""
    return front_size(partition_size, erase_size) - slot_overhead(erase_size)


def is_ota_capable(partition_size: int, erase_size: int) -> bool:
    """Whether a partition can host A/B OTA (each of two slots has a non-empty body)."""
    return body_capacity(partition_size, erase_size) > 0


# --- v2: mode selection ------------------------------------------------------
#
# A/B is the default wherever the geometry allows it. SINGLE exists for the legacy boards whose
# whole ROMFS partition is one erase sector (OpenMV2/3/4), where two slots are arithmetically
# impossible -- so it must work, but it does not get to shape the A/B design.
#
# WHY SINGLE IS POSSIBLE AT ALL on a one-sector board: the control sectors cannot be erased
# independently of the body there -- there is only one erase unit. That would be fatal if the
# control data had to be REWRITTEN, but it does not. The install invariant is one erase pass
# followed by writes only, and every control write is a 1->0 program: status markers are written
# once each as the trial advances, and the rollback log is append-only. So body and control can
# share an erase block, provided nothing ever needs to erase one without the other -- which is
# exactly the invariant v2 already commits to keeping.

SINGLE = "single"
AB = "ab"


# Control area for SINGLE mode: THE SAME FOUR 4 KiB SECTORS AS A/B.
#
# What made the one-sector boards look impossible was never the number of control sectors, it was
# sizing each one by the erase block -- four 128 KiB sectors demanded 512 KiB of a 128 KiB
# partition. With ``control_block()`` fixed at 4 KiB that is 16 KiB, which a 128 KiB partition can
# host with 112 KiB left for the image.
#
# So SINGLE keeps the identical layout rather than packing the four records into one block. The
# packing would save 12 KiB on boards that are a curiosity (OpenMV2/3/4), and it would cost a
# second on-flash layout for boot.py, the installer and the builder to agree on -- a permanent
# source of drift for a one-off saving. One layout, both modes.
SINGLE_CONTROL_BYTES = CONTROL_SECTORS * MIN_OTA_BLOCK


def single_body_capacity(partition_size: int, erase_size: int) -> int:
    """Usable image bytes in SINGLE mode: the partition less its packed control area.

    ``<= 0`` means the partition cannot host OTA in any mode."""
    del erase_size                      # deliberately unused: control is not erase-aligned here
    return int(partition_size) - SINGLE_CONTROL_BYTES


def is_single_capable(partition_size: int, erase_size: int) -> bool:
    """Whether a partition can host OTA in SINGLE mode."""
    return single_body_capacity(partition_size, erase_size) > 0


def derive_mode(partition_size: int, erase_size: int) -> str | None:
    """The mode this geometry supports, with no user input: ``AB`` when two slots fit,
    ``SINGLE`` when only one does, ``None`` when the partition cannot host OTA at all.

    Deriving rather than asking is deliberate: A/B is the safe default and should require no
    decision, and a maker who opts out should have to say so explicitly (see
    ``resolve_mode``)."""
    if is_ota_capable(partition_size, erase_size):
        return AB
    if is_single_capable(partition_size, erase_size):
        return SINGLE
    return None


def resolve_mode(partition_size: int, erase_size: int, single_image: bool = False) -> str:
    """The mode to build, honouring an explicit ``single_image`` opt-out.

    Asymmetric on purpose. Opting DOWN to single-image is allowed on any capable partition --
    it buys back a full image of flash, which a maker may want for models or data. Opting UP is
    not a thing: a board without room for two slots raises rather than silently pretending, and
    a partition that cannot host OTA at all raises whatever was asked.

    The cost of opting out is invisible when you choose it and expensive years later: without a
    B slot a failed update needs a network round trip to recover, and a device that cannot reach
    the network needs physical reflashing. Hence the caller-facing name is ``single_image``, which
    states what you get, rather than ``ab=False``, which reads as a preference."""
    derived = derive_mode(partition_size, erase_size)
    if derived is None:
        raise ValueError(
            "partition of %d bytes with a %d-byte erase block cannot host OTA in any mode: "
            "one slot needs %d bytes of control sectors alone"
            % (partition_size, erase_size, slot_overhead(erase_size)))
    if single_image:
        return SINGLE
    return derived
