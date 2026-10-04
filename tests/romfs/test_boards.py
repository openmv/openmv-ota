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
    assert p.erase_size == 4096  # external XSPI NOR
    rules = {r["extension"]: r["alignment"] for r in p.alignment_rules}
    assert rules["tflite"] == 32  # N6 uses 32-byte alignment


def test_ae3_has_two_partitions():
    b = boards_mod.get_board("OPENMV_AE3")
    assert len(b.partitions) == 2
    assert {p.index for p in b.partitions} == {0, 1}
    assert b.partition(1).size == 1048576  # 1 MiB MRAM (HE core)
    assert b.partition(0).erase_size == 4096  # OSPI NOR
    assert b.partition(1).erase_size == 16    # MRAM (byte-writable)


def test_internal_flash_boards_have_large_erase():
    # Single-sector internal flash -> not OTA-capable (proven by geometry).
    assert boards_mod.get_board("OPENMV4").partition().erase_size == 131072
    assert boards_mod.get_board("OPENMV3").partition().erase_size == 262144


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


def test_unsupported_reason():
    # retired boards carry a reason; supported and unknown boards return None
    assert "crashes at boot" in boards_mod.unsupported_reason("ARDUINO_NANO_RP2040_CONNECT")
    assert boards_mod.unsupported_reason("OPENMV4") is None
    assert boards_mod.unsupported_reason("NOPE") is None        # unknown -> None, not a crash


def test_cloud_capability_per_board():
    """The hosted-cloud level the website's board picker shows. The six boards proven on the
    hosted cloud plus the H7 Plus (WINC1500 shield) run all of it with QVGA Live video; the
    discontinued M4/M7/H7 join unverified, the H7 at QVGA and the M4/M7 at QQVGA for now."""
    from openmv_ota.romfs.boards import CLOUD_LEVELS, load_boards

    boards = load_boards()
    qvga = {"OPENMV_N6", "OPENMV_AE3", "OPENMV_RT1060", "OPENMV4P", "ARDUINO_NICLA_VISION",
            "ARDUINO_GIGA", "ARDUINO_PORTENTA_H7", "OPENMV4"}
    qqvga = {"OPENMV3", "OPENMV2"}
    assert {n for n, b in boards.items() if b.cloud == "full"} == qvga | qqvga
    assert all(b.cloud in CLOUD_LEVELS + (None,) for b in boards.values())
    assert all(boards[n].live_framesize == "QVGA" for n in qvga)
    assert all(boards[n].live_framesize == "QQVGA" for n in qqvga)
    assert boards["OPENMVPT"].cloud is None and boards["OPENMVPT"].live_framesize is None


def test_only_the_discontinued_classics_skip_tls_verification():
    """tls_verify false is the M4/M7/H7 and nothing else: every other board verifies, and a
    board with no entry defaults to verifying."""
    from openmv_ota.romfs.boards import load_boards

    boards = load_boards()
    assert {n for n, b in boards.items() if not b.tls_verify} == {"OPENMV2", "OPENMV3", "OPENMV4"}
    assert boards_mod.BoardConfig("X", "X", "", [], []).tls_verify is True


def test_an_unknown_cloud_level_is_refused():
    from openmv_ota.romfs import boards as boards_mod

    assert boards_mod._cloud_level("X", None) is None
    assert boards_mod._cloud_level("X", "no-live") == "no-live"
    with pytest.raises(ValueError, match="X has cloud 'live'"):
        boards_mod._cloud_level("X", "live")
