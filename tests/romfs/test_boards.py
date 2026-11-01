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


def test_stub_layout_is_parsed_where_a_board_has_one():
    from openmv_ota.romfs import boards as B
    n6 = B.get_board("OPENMV_N6").stub
    assert n6 is not None
    # the N6's FIRMWARE partition is 0x80000..0x400000 of the XIP flash at 0x70000000: the stub
    # takes its first 64 KiB, the install record its last 4 KiB, the firmware the rest
    assert (n6.origin, n6.firmware_dest, n6.firmware_max) == (
        0x70080000, 0x70090000, 0x703FF000 - 0x70090000)
    assert n6.stub_max == 0x10000                              # one 64 KiB erase block
    assert n6.record_addr == 0x703FF000 and n6.coproc_dest is None and n6.record is None
    assert B.get_board("OPENMV4") .stub is None                # 128 KiB romfs: no room for a firmware


def test_the_rt1060_stub_layout_keeps_the_images_header_block():
    """The RT1062's resident bootloader jumps to the vectors at +0x2000 of the image that
    started at 0x60040000, so the stub's region starts there (its head 8 KiB in) and the
    firmware moves up one 64 KiB region with its header block intact. The record takes the
    sector after the firmware window, the default, so boards.json names none."""
    from openmv_ota.romfs import boards as B
    rt = B.get_board("OPENMV_RT1060").stub
    assert (rt.origin, rt.firmware_dest, rt.firmware_max) == (0x60040000, 0x60050000, 0x603FF000 - 0x60050000)
    assert rt.stub_max == 0x10000
    assert rt.record_addr == 0x603FF000 == rt.firmware_dest + rt.firmware_max
    assert rt.text2_dest is None and rt.coproc_dest is None
    assert B.firmware_image_name("OPENMV_RT1060") == "OPENMV_RT1060-firmware.bin"


def test_the_ae3_stub_layout_spans_both_cores():
    """MRAM has no erase blocks, so the stub takes 32 KiB of the HP partition and the HP
    firmware follows it; the HE image is a second component in its own partition, and the
    install record takes the last 4 KiB of the HE partition (the last install window), so
    firmware_dest + firmware_max is NOT where it lives."""
    from openmv_ota.romfs import boards as B
    ae3 = B.get_board("OPENMV_AE3").stub
    assert (ae3.origin, ae3.firmware_dest, ae3.firmware_max) == (
        0x80020000, 0x80028000, 0x80320000 - 0x80028000)
    assert ae3.stub_max == 0x8000
    assert (ae3.coproc_dest, ae3.coproc_max) == (0x80320000, 0x8047D000 - 0x80320000)
    assert ae3.record_addr == 0x8047D000 == ae3.coproc_dest + ae3.coproc_max
    assert ae3.text2_dest is None
    assert B.firmware_image_name("OPENMV_AE3") == "OPENMV_AE3-firmware-M55_HP.bin"
    assert B.coprocessor_image_name("OPENMV_AE3") == "OPENMV_AE3-firmware-M55_HE.bin"
    assert B.firmware_image_name("OPENMV_N6") == "OPENMV_N6-firmware.bin"
    assert B.coprocessor_image_name("OPENMV_N6") == "OPENMV_N6-firmware-M55_HE.bin"   # the default name
    assert B.firmware_image_name("ARDUINO_GIGA") == "ARDUINO_GIGA-firmware.bin"     # no flash.file map


def test_a_stub_coprocessor_window_and_record_are_validated():
    from openmv_ota.romfs import boards as B
    base = {"origin": "0x80020000", "firmware_dest": "0x80028000", "firmware_max": "0x2F8000"}
    ok = B._stub("X", {**base, "coprocessor": {"dest": "0x80320000", "max": "0x15D000"},
                       "record": "0x8047D000"})
    assert (ok.coproc_dest, ok.coproc_max, ok.record_addr) == (0x80320000, 0x15D000, 0x8047D000)
    with pytest.raises(ValueError, match="bad stub coprocessor window"):
        B._stub("X", {**base, "coprocessor": {"dest": "0x80320000"}})
    with pytest.raises(ValueError, match="must follow the firmware window"):
        B._stub("X", {**base, "coprocessor": {"dest": "0x80300000", "max": "0x1000"}})
    with pytest.raises(ValueError, match="must follow the firmware window"):
        B._stub("X", {**base, "coprocessor": {"dest": "0x80320000", "max": "0"}})
    with pytest.raises(ValueError, match="bad stub record address"):
        B._stub("X", {**base, "record": "x"})
    for bad in ("0x80028100",      # not a sector
                "0x80010000",      # below the stub
                "0x80100000",      # inside the firmware window
                "0x80400000"):     # inside the coprocessor window
        with pytest.raises(ValueError, match="outside the stub and the install windows"):
            B._stub("X", {**base, "coprocessor": {"dest": "0x80320000", "max": "0x15D000"},
                          "record": bad})
    assert B._stub("X", {**base, "record": "0x80320000"}).record_addr == 0x80320000   # no coprocessor


def test_the_stm32h7_boards_share_one_stub_layout():
    """Internal flash in 128 KiB sectors: the stub takes the FIRMWARE area's first sector
    (0x08040000), the firmware moves up one sector, and the install record takes the last
    4 KiB of the last sector -- firmware_max stops short of it, as on the N6."""
    from openmv_ota.romfs import boards as B
    for name in ("OPENMV4P", "OPENMVPT", "ARDUINO_PORTENTA_H7", "ARDUINO_GIGA",
                 "ARDUINO_NICLA_VISION"):
        stub = B.get_board(name).stub
        assert stub is not None, name
        assert (stub.origin, stub.firmware_dest, stub.firmware_max) == (
            0x08040000, 0x08060000, 0x081FF000 - 0x08060000), name
        assert stub.stub_max == 0x20000                                 # one 128 KiB sector
        assert stub.firmware_dest + stub.firmware_max == 0x081FF000     # the record's 4 KiB
        # the sector the bootloader jumps past (the unused internal filesystem's) is the
        # firmware's second text region, so no board drops a feature to fit behind the stub
        assert (stub.text2_dest, stub.text2_max) == (0x08020000, 0x20000), name
        assert stub.text2_prefer == () and stub.text2_exclude == ()
        assert B.get_board(name).ota_firmware_drops == {}, name
    assert B.get_board("OPENMV_N6").stub.text2_dest is None            # one window is plenty there


def test_a_stub_text2_region_is_validated():
    from openmv_ota.romfs import boards as B
    base = {"origin": "0x08040000", "firmware_dest": "0x08060000", "firmware_max": "0x19F000"}
    ok = B._stub("X", {**base, "text2": {"dest": "0x08020000", "max": "0x20000",
                                         "prefer": ["lib/imlib/"], "exclude": ["main.o"]}})
    assert (ok.text2_dest, ok.text2_max, ok.text2_prefer, ok.text2_exclude) == (
        0x08020000, 0x20000, ("lib/imlib/",), ("main.o",))
    with pytest.raises(ValueError, match="bad stub text2 region"):
        B._stub("X", {**base, "text2": {"dest": "0x08020000"}})
    with pytest.raises(ValueError, match="must not overlap"):
        B._stub("X", {**base, "text2": {"dest": "0x08050000", "max": "0x1000"}})   # the stub's sector
    with pytest.raises(ValueError, match="must not overlap"):
        B._stub("X", {**base, "text2": {"dest": "0x08020000", "max": "0"}})
    with pytest.raises(ValueError, match="non-empty strings"):
        B._stub("X", {**base, "text2": {"dest": "0x08020000", "max": "0x20000", "prefer": [""]}})
    assert B._stub("X", None) is None
    with pytest.raises(ValueError, match="bad stub layout"):
        B._stub("X", {"firmware_dest": "0x10"})
    with pytest.raises(ValueError, match="origin < firmware_dest"):
        B._stub("X", {"origin": "0x20", "firmware_dest": "0x10", "firmware_max": 4})


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
    hosted cloud plus the H7 Plus (WINC1500 shield) and the discontinued H7 run all of it with
    QVGA Live video. The M7 drops Live to fit its RAM ("no-live", pending its bench results);
    the M4's runtime drops the network stack, so it has no cloud level."""
    from openmv_ota.romfs.boards import CLOUD_LEVELS, load_boards

    boards = load_boards()
    qvga = {"OPENMV_N6", "OPENMV_AE3", "OPENMV_RT1060", "OPENMV4P", "ARDUINO_NICLA_VISION",
            "ARDUINO_GIGA", "ARDUINO_PORTENTA_H7", "OPENMV4"}
    assert {n for n, b in boards.items() if b.cloud == "full"} == qvga
    assert all(b.cloud in CLOUD_LEVELS + (None,) for b in boards.values())
    assert all(boards[n].live_framesize == "QVGA" for n in qvga)
    assert boards["OPENMV3"].cloud is None and boards["OPENMV3"].live_framesize is None   # heap: no TLS
    assert boards["OPENMVPT"].cloud is None and boards["OPENMVPT"].live_framesize is None
    assert boards["OPENMV2"].cloud is None


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


def test_live_framesize_belongs_to_full_boards_only():
    """Only a "full" board streams Live, so only it names a Live frame size -- and it must
    name one the device SDK knows how to scale to."""
    from openmv_ota.romfs import boards as boards_mod

    assert boards_mod._live_framesize("X", "full", "QQVGA") == "QQVGA"
    assert boards_mod._live_framesize("X", "no-live", None) is None
    assert boards_mod._live_framesize("X", None, None) is None
    with pytest.raises(ValueError, match="X has live_framesize 'QVGA' but cloud 'no-live'"):
        boards_mod._live_framesize("X", "no-live", "QVGA")
    with pytest.raises(ValueError, match="X is cloud \"full\" and needs live_framesize"):
        boards_mod._live_framesize("X", "full", None)
    with pytest.raises(ValueError, match="needs live_framesize"):
        boards_mod._live_framesize("X", "full", "HD")
