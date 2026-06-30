"""`flash bootloader` -- STM32 system-DFU path + the clear messages for the rest."""

from __future__ import annotations

import pytest

from openmv_ota.cli import main
from openmv_ota.flash import dfu
from openmv_ota.flash import flash as fl
from openmv_ota.flash.errors import FlashError


def test_bootloader_argv_address_based_no_reset():
    from pathlib import Path
    argv = dfu.bootloader_argv("dfu-util", "0483:df11", 0, "0x08000000", Path("b.bin"))
    assert argv == ["dfu-util", "-w", "-d", ",0483:df11", "-a", "0", "-s", "0x08000000",
                    "-D", "b.bin"]
    assert "--reset" not in argv and ":leave" not in " ".join(argv)
    pinned = dfu.bootloader_argv("dfu-util", "0483:df11", 0, "0x08000000", Path("b.bin"),
                                 serial="SN1")
    assert pinned[4:6] == ["-S", "SN1"]


@pytest.fixture
def bl_project(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    ran: list = []
    monkeypatch.setattr(fl.runner, "run", lambda argv, **k: ran.append((argv, k)))
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: override or "DFU")
    monkeypatch.setattr(fl.history, "record", lambda *a, **k: None)
    (tmp_path / "build" / "OPENMV4-bootloader.bin").write_bytes(b"BOOT")
    return tmp_path, ran


def test_bootloader_flash_via_system_dfu(bl_project, capsys):
    root, ran = bl_project
    steps = fl.flash_bootloader(str(root), board="OPENMV4")
    # the system DFU id + 0x08000000, and the ST-ROM exit quirk is tolerated
    assert ran[0][0][:4] == ["DFU", "-w", "-d", ",0483:df11"]
    assert ran[0][0][4:] == ["-a", "0", "-s", "0x08000000", "-D",
                             str(root / "build/OPENMV4-bootloader.bin")]
    assert ran[0][1] == {"tolerate_fail": True}
    assert steps[0].artifact == "bootloader"
    # the manual BOOT0 instruction is shown
    assert "jumper BOOT0 (the REC pad) to 3.3V" in capsys.readouterr().err


def test_bootloader_no_auto_reset(bl_project, monkeypatch):
    # bootloader entry is manual (system DFU); never the mpremote/touch path
    root, _ran = bl_project
    monkeypatch.setattr(fl.device, "reset", lambda *a, **k: pytest.fail("must not auto-reset"))
    fl.flash_bootloader(str(root), board="OPENMV4")


def test_bootloader_dry_run_shows_instructions_runs_nothing(bl_project, capsys):
    root, ran = bl_project
    fl.flash_bootloader(str(root), board="OPENMV4", dry_run=True)
    assert ran == []
    assert "jumper BOOT0 (the REC pad) to 3.3V" in capsys.readouterr().err


def test_bootloader_missing_artifact(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: "DFU")
    with pytest.raises(FlashError, match="OPENMV4-bootloader.bin"):
        fl.flash_bootloader(str(tmp_path), board="OPENMV4")


def test_bootloader_non_dfu_backends_give_clear_notes():
    with pytest.raises(FlashError, match="STM32CubeProgrammer"):
        fl.flash_bootloader(board="OPENMV_N6")
    with pytest.raises(FlashError, match="flash factory"):
        fl.flash_bootloader(board="OPENMV_RT1060")
    with pytest.raises(FlashError, match="Alif SE tools"):
        fl.flash_bootloader(board="OPENMV_AE3")


def test_bootloader_not_available_for_arduino():
    with pytest.raises(FlashError, match="no bootloader to flash"):
        fl.flash_bootloader(board="ARDUINO_PORTENTA_H7")


def test_bootloader_cli(bl_project, capsys):
    root, _ran = bl_project
    assert main(["flash", "bootloader", str(root), "-b", "OPENMV4", "--dry-run"]) == 0
    out = capsys.readouterr()
    assert "would run: DFU -w -d ,0483:df11" in out.out
    assert "jumper BOOT0 (the REC pad) to 3.3V" in out.err


def test_bootloader_cli_error_returns_exit_code(bl_project, capsys):
    root, _ran = bl_project
    assert main(["flash", "bootloader", str(root), "-b", "OPENMV_AE3"]) == 2
    assert "Alif SE tools" in capsys.readouterr().err
