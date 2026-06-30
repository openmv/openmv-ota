"""Resolving a board to its flash backend + alt map from boards.json."""

from __future__ import annotations

import pytest

from openmv_ota.flash import targets
from openmv_ota.flash.errors import FlashError
from openmv_ota.romfs.boards import BoardConfig


def test_dfu_board_resolves():
    cfg = targets.flash_config("OPENMV4")
    assert cfg.backend == "dfu" and cfg.usb == "37c5:9204"
    assert cfg.alt_of("firmware") == 2 and cfg.alt_of("romfs") == 3


def test_n6_has_firmware_at_alt_1():
    # N6's table orders FIRMWARE before FILESYSTEM, unlike the H7 boards -- the map is explicit.
    cfg = targets.flash_config("OPENMV_N6")
    assert cfg.alt_of("firmware") == 1 and cfg.alt_of("romfs") == 3


def test_ae3_is_dfu_with_per_core_alts():
    # AE3 flashes everything over DFU (write-mram is only for its bootloader): HP fw, HE fw,
    # the coprocessor romfs, and the main romfs -- and its firmware file is the per-core one.
    cfg = targets.flash_config("OPENMV_AE3")
    assert cfg.backend == "dfu" and cfg.usb == "37c5:96e3"
    assert (cfg.alt_of("firmware"), cfg.alt_of("coprocessor"),
            cfg.alt_of("coprocessor_romfs"), cfg.alt_of("romfs")) == (1, 2, 3, 6)
    assert cfg.filename("firmware", "firmware.bin") == "firmware-M55_HP.bin"
    assert cfg.filename("romfs", "romfs.img") == "romfs.img"   # no override -> default


def test_alt_of_unknown_artifact_raises():
    cfg = targets.flash_config("OPENMV4")
    with pytest.raises(FlashError, match="no 'coprocessor' flash target"):
        cfg.alt_of("coprocessor")


def test_arduino_backend_resolves():
    cfg = targets.flash_config("ARDUINO_PORTENTA_H7")
    assert cfg.backend == "arduino" and cfg.usb == "2341:035b"
    assert cfg.raw["firmware"]["addr"] == "0x08040000"
    assert cfg.raw["app"]["usb"] == "2341:005b"


def test_unknown_board_raises():
    with pytest.raises(FlashError):
        targets.flash_config("NOPE")


def test_board_without_flash_block_raises():
    # a QEMU board has no flash block -- not a physical flashable target.
    with pytest.raises(FlashError, match="no flash configuration"):
        targets.flash_config("MPS2_AN500")


def test_unsupported_board_gives_a_reason():
    # the pico / ble33 use a UF2 bootloader, not our DFU path -- a clean, specific error
    with pytest.raises(FlashError, match="UF2 mass-storage bootloader"):
        targets.flash_config("ARDUINO_NANO_RP2040_CONNECT")
    with pytest.raises(FlashError, match="can't be flashed with this tool"):
        targets.flash_config("ARDUINO_NANO_33_BLE_SENSE")


def test_unsupported_backend_raises(monkeypatch):
    fake = BoardConfig(name="FAKE", display_name="Fake", arch="x", mpy_args=[],
                       partitions=[], flash={"backend": "alif", "usb": "37c5:96e3", "alt": {}})
    monkeypatch.setattr(targets, "get_board", lambda _n: fake)
    with pytest.raises(FlashError, match="not supported yet"):
        targets.flash_config("FAKE")
