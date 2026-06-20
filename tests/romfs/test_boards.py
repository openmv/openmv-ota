"""Tests for board-config loading."""

from __future__ import annotations

import pytest

from openmv_ota.romfs import boards as boards_mod


def test_load_boards_has_expected_entries():
    boards = boards_mod.load_boards()
    for name in ("OPENMV_N6", "OPENMV_AE3", "ARDUINO_NICLA_VISION"):
        assert name in boards


def test_n6_partition_and_alignment():
    b = boards_mod.get_board("OPENMV_N6")
    p = b.partition()
    assert p.index == 0
    assert p.size == 25165824  # 24 MiB
    rules = {r["extension"]: r["alignment"] for r in p.alignment_rules}
    assert rules["tflite"] == 32  # N6 uses 32-byte alignment


def test_ae3_has_two_partitions():
    b = boards_mod.get_board("OPENMV_AE3")
    assert len(b.partitions) == 2
    assert {p.index for p in b.partitions} == {0, 1}
    assert b.partition(1).size == 1048576  # 1 MiB MRAM (HE core)


def test_partition_bad_index():
    b = boards_mod.get_board("OPENMV_N6")
    with pytest.raises(LookupError):
        b.partition(5)


def test_unknown_board_lists_known():
    with pytest.raises(KeyError) as ei:
        boards_mod.get_board("NOPE")
    assert "OPENMV_N6" in str(ei.value)


def test_board_names_sorted():
    names = boards_mod.board_names()
    assert names == sorted(names)
    assert "OPENMV_N6" in names
