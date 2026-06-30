"""`flash erase`: download a 4 KB sector of zeros to each erase target (the IDE eraseCommands)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openmv_ota.cli import main
from openmv_ota.flash import dfu
from openmv_ota.flash import flash as fl
from openmv_ota.flash.errors import FlashError


def test_erase_argv_alt_only_and_address_forms():
    a = dfu.erase_argv("dfu-util", "37c5:9204", {"alt": 1}, Path("z.bin"))
    assert a == ["dfu-util", "-w", "-d", ",37c5:9204", "-a", "1", "-D", "z.bin"]
    # alt-only leave -> --reset; serial pins the board
    a2 = dfu.erase_argv("dfu-util", "37c5:9204", {"alt": 1}, Path("z.bin"), leave=True, serial="SN")
    assert a2[4:6] == ["-S", "SN"] and "--reset" in a2 and ":leave" not in " ".join(a2)
    # address-based (arduino) leave -> :leave on the addr, never --reset
    b = dfu.erase_argv("dfu-util", "2341:035b", {"alt": 1, "addr": "0x90000000"}, Path("z.bin"),
                       leave=True)
    assert b[b.index("-s") + 1] == "0x90000000:leave" and "--reset" not in b


@pytest.fixture
def erase_project(tmp_path, monkeypatch):
    ran: list = []
    monkeypatch.setattr(fl.runner, "run", lambda argv, **k: ran.append(argv))
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: override or "DFU")
    monkeypatch.setattr(fl.history, "record", lambda *a, **k: None)
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: None)   # already in bootloader
    return tmp_path, ran


def test_erase_openmv_downloads_zeros_to_the_fs_alt(erase_project):
    root, ran = erase_project
    steps = fl.flash_erase(str(root), board="OPENMV4")
    assert len(ran) == 1
    assert ran[0][:4] == ["DFU", "-w", "-d", ",37c5:9204"]
    assert ran[0][4:7] == ["-a", "1", "--reset"]      # H7 filesystem alt 1, reset on the last step
    assert ran[0][-2] == "-D"
    assert [s.label for s in steps] == ["erase alt 1"]


def test_erase_downloads_a_4kb_zero_sector(erase_project, monkeypatch):
    root, _ran = erase_project
    seen: dict = {}
    monkeypatch.setattr(fl.runner, "run",
                        lambda argv, **k: seen.update(data=Path(argv[-1]).read_bytes()))
    fl.flash_erase(str(root), board="OPENMV_AE3")
    assert seen["data"] == b"\x00" * 4096


def test_erase_arduino_walks_both_targets_leave_on_last(erase_project):
    root, ran = erase_project
    steps = fl.flash_erase(str(root), board="ARDUINO_PORTENTA_H7")
    assert len(ran) == 2
    assert ran[0][4:8] == ["-a", "0", "-s", "0x08020000"] and "--reset" not in ran[0]
    assert ran[1][4:8] == ["-a", "1", "-s", "0x90000000:leave"]
    assert [s.label for s in steps] == ["erase alt 0", "erase alt 1"]


def test_erase_no_reset_leaves_board_in_bootloader(erase_project):
    root, ran = erase_project
    fl.flash_erase(str(root), board="OPENMV4", reset=False)
    assert "--reset" not in ran[0] and ":leave" not in " ".join(ran[0])


def test_erase_dry_run_runs_nothing(erase_project):
    root, ran = erase_project
    steps = fl.flash_erase(str(root), board="OPENMV_N6", dry_run=True)
    assert ran == []
    assert steps[0].argv[:4] == ["DFU", "-w", "-d", ",37c5:9206"]
    assert steps[0].argv[4:7] == ["-a", "2", "--reset"]


def test_erase_refused_for_imx_backend():
    with pytest.raises(FlashError, match="erase isn't available for the 'imx' backend"):
        fl.flash_erase(board="OPENMV_RT1060")


def test_erase_refused_for_retired_nano():
    with pytest.raises(FlashError, match="no longer supported"):
        fl.flash_erase(board="ARDUINO_NANO_RP2040_CONNECT")


def test_erase_missing_target_configured(monkeypatch):
    from openmv_ota.flash.targets import FlashConfig
    monkeypatch.setattr(fl, "flash_config",                   # a dfu board with no erase block
                        lambda b: FlashConfig(board=b, backend="dfu", raw={"backend": "dfu"}))
    with pytest.raises(FlashError, match="no erase target configured"):
        fl.flash_erase(board="SOMEBOARD")


def test_erase_cli_dry_run(erase_project, capsys):
    root, _ran = erase_project
    assert main(["flash", "erase", str(root), "-b", "OPENMV4", "--dry-run"]) == 0
    assert "would run: DFU -w -d ,37c5:9204 -a 1 --reset -D" in capsys.readouterr().out


def test_erase_cli_error_returns_exit_code(capsys):
    assert main(["flash", "erase", "-b", "OPENMV_RT1060"]) == 2
    assert "erase isn't available" in capsys.readouterr().err
