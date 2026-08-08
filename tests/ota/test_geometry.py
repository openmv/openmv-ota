"""Tests for OTA slot geometry."""

from __future__ import annotations

import pytest

from openmv_ota.ota import geometry


def test_ota_block_floors_to_4k():
    assert geometry.ota_block(16) == 4096       # MRAM's tiny sector floored up
    assert geometry.ota_block(4096) == 4096     # NOR sector unchanged
    assert geometry.ota_block(131072) == 131072  # large internal sector unchanged


def test_front_size_aligns_down_to_block():
    # 24 MiB NOR partition: half, 4 KiB-aligned.
    assert geometry.front_size(0x1800000, 4096) == 0xC00000
    # A half that isn't block-aligned rounds down.
    assert geometry.front_size(0x1800000 + 0x1000, 4096) == 0xC00000


def test_slot_overhead_is_four_blocks():
    # rollback + spare + status + trailer, each one block
    assert geometry.slot_overhead(4096) == 4 * 4096
    assert geometry.slot_overhead(16) == 4 * 4096   # floored block -> 4 x 4 KiB
    assert geometry.slot_overhead(131072) == 4 * 131072


def test_control_sector_offsets():
    # the control sectors are the last four blocks, in fixed order
    assert geometry.trailer_offset(0x100000, 4096) == 0x100000 - 4096
    assert geometry.status_offset(0x100000, 4096) == 0x100000 - 2 * 4096
    assert geometry.rollback_offset(0x100000, 4096) == 0x100000 - 3 * 4096


def test_capable_nor_partition():
    assert geometry.is_ota_capable(0x1800000, 4096)         # 24 MiB NOR
    assert geometry.body_capacity(0x1800000, 4096) == 0xC00000 - 4 * 4096


def test_mram_partition_capable_with_floor():
    # AE3 MRAM: 1 MiB, 16-byte physical sector -> floored to 4 KiB blocks, OTA-capable.
    assert geometry.is_ota_capable(0x100000, 16)
    assert geometry.body_capacity(0x100000, 16) == 0x80000 - 4 * 4096


def test_single_sector_partition_not_capable():
    # OpenMV4: 128 KiB romfs is one 128 KiB erase sector -> a slot rounds to 0.
    assert geometry.front_size(0x20000, 0x20000) == 0
    assert not geometry.is_ota_capable(0x20000, 0x20000)
    assert geometry.body_capacity(0x20000, 0x20000) <= 0
    # OpenMV3: 256 KiB romfs, 256 KiB sector -> same.
    assert not geometry.is_ota_capable(0x40000, 0x40000)


# --- v2 mode selection ------------------------------------------------------------------
# A/B is the default wherever it fits; SINGLE exists for the legacy one-sector boards. The
# asymmetry is deliberate: opting DOWN is a maker's choice, opting UP is not a thing.

def test_ab_is_derived_wherever_two_slots_fit():
    from openmv_ota.ota import geometry as g

    assert g.derive_mode(12 * 1024 * 1024, 4096) == g.AB      # N6-class
    assert g.derive_mode(4 * 1024 * 1024, 4096) == g.AB


def test_one_sector_boards_derive_single_rather_than_nothing():
    """The whole point of the mode. These boards were arithmetically excluded before, because
    the control sectors were sized by the ERASE BLOCK so they could be erased independently --
    four 128 KiB sectors in a 128 KiB partition. SINGLE never erases control separately from the
    body, so the area only has to HOLD the data."""
    from openmv_ota.ota import geometry as g

    assert g.is_ota_capable(128 * 1024, 128 * 1024) is False   # no room for two slots
    assert g.derive_mode(128 * 1024, 128 * 1024) == g.SINGLE
    assert g.single_body_capacity(128 * 1024, 128 * 1024) == 128 * 1024 - g.SINGLE_CONTROL_BYTES


def test_a_partition_too_small_for_control_hosts_nothing():
    from openmv_ota.ota import geometry as g

    assert g.derive_mode(4096, 4096) is None
    with pytest.raises(ValueError, match="cannot host OTA in any mode"):
        g.resolve_mode(4096, 4096)


def test_single_image_opt_out_is_honoured_on_a_capable_partition():
    """Buying back a full image of flash is a legitimate choice; it is just not the default."""
    from openmv_ota.ota import geometry as g

    assert g.resolve_mode(12 * 1024 * 1024, 4096) == g.AB                      # default
    assert g.resolve_mode(12 * 1024 * 1024, 4096, single_image=True) == g.SINGLE


def test_there_is_no_way_to_opt_UP_into_ab():
    """A board without room for two slots must not be able to ask for A/B and get a silent
    downgrade -- resolve_mode simply never returns AB for such a partition."""
    from openmv_ota.ota import geometry as g

    assert g.resolve_mode(128 * 1024, 128 * 1024) == g.SINGLE
    assert g.resolve_mode(128 * 1024, 128 * 1024, single_image=True) == g.SINGLE
