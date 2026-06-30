"""Tests for OTA slot geometry."""

from __future__ import annotations

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
