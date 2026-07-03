"""Firmware board name -> swd-ids code translation."""

from __future__ import annotations

from openmv_ota.server.boardmap import swd_ids_board_code


def test_known_firmware_names_map_to_codes():
    assert swd_ids_board_code("OPENMV2") == "M4"
    assert swd_ids_board_code("OPENMV3") == "M7"
    assert swd_ids_board_code("OPENMV4") == "H7"
    assert swd_ids_board_code("OPENMV_N6") == "N6"
    assert swd_ids_board_code("OPENMV_RT1060") == "IMXRT1060"
    assert swd_ids_board_code("ARDUINO_PORTENTA_H7") == "H7"
    assert swd_ids_board_code("ARDUINO_GIGA") == "H7"
    assert swd_ids_board_code("ARDUINO_NICLA_VISION") == "NICLAV"


def test_unknown_board_passes_through():
    assert swd_ids_board_code("SOMETHING_ELSE") == "SOMETHING_ELSE"
    assert swd_ids_board_code(None) is None                      # missing board -> passes through


def test_overrides_add_and_beat_defaults():
    assert swd_ids_board_code("SOME_NEW_BOARD", {"SOME_NEW_BOARD": "XX"}) == "XX"  # override adds a mapping
    assert swd_ids_board_code("OPENMV_N6", {"OPENMV_N6": "XYZ"}) == "XYZ"          # override beats the default
    assert swd_ids_board_code("OPENMV_N6", {}) == "N6"                            # empty overrides -> default
