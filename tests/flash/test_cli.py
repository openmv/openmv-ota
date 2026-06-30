"""The ``openmv-ota flash`` command group end-to-end (argv parsing -> handler -> report)."""

from __future__ import annotations

import pytest

from openmv_ota.cli import main
from openmv_ota.flash import flash as fl


@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    ran: list[list[str]] = []
    monkeypatch.setattr(fl.runner, "run", lambda argv: ran.append(argv))
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: override or "DFU")
    monkeypatch.setattr(fl.history, "record", lambda *a, **k: None)

    def artifact(name):
        (tmp_path / "build" / name).write_bytes(b"x")

    return tmp_path, ran, artifact


def test_flash_firmware_ok(proj, capsys):
    root, ran, artifact = proj
    artifact("OPENMV4-firmware.bin")
    assert main(["flash", "firmware", str(root), "-b", "OPENMV4"]) == 0
    assert len(ran) == 1
    assert "Flashed OPENMV4-firmware.bin -> alt 2 (OPENMV4)" in capsys.readouterr().out


def test_flash_factory_ok(proj, capsys):
    root, ran, artifact = proj
    artifact("OPENMV4-firmware.bin")
    artifact("OPENMV4-factory-romfs.img")
    assert main(["flash", "factory", str(root), "-b", "OPENMV4"]) == 0
    assert len(ran) == 2


def test_flash_romfs_dry_run_prints_command(proj, capsys):
    root, ran, artifact = proj
    artifact("OPENMV4-romfs.img")
    assert main(["flash", "romfs", str(root), "-b", "OPENMV4", "--dry-run",
                 "--dfu-util", "/x/dfu-util"]) == 0
    assert ran == []
    assert "would run: /x/dfu-util -d 37c5:9204 -a 3" in capsys.readouterr().out


def test_error_returns_exit_code(proj, capsys):
    root, _ran, _artifact = proj
    rc = main(["flash", "firmware", str(root), "-b", "OPENMV4"])   # no artifact built
    assert rc == 2
    assert "error: missing artifact" in capsys.readouterr().err


def test_romfs_error_returns_exit_code(proj, capsys):
    root, _ran, _artifact = proj
    assert main(["flash", "romfs", str(root), "-b", "OPENMV4"]) == 2   # no artifact built
    assert "error: missing artifact" in capsys.readouterr().err


def test_unsupported_board_error(proj, capsys):
    root, _ran, _artifact = proj
    assert main(["flash", "firmware", str(root), "-b", "OPENMV_RT1060"]) == 2
    assert "no flash configuration" in capsys.readouterr().err


def test_sdk_home_passed_through(proj, monkeypatch, capsys):
    root, ran, artifact = proj
    artifact("OPENMV4-romfs.img")
    seen = {}
    monkeypatch.setattr(fl.tools, "find_dfu_util",
                        lambda override, sdk_home: seen.setdefault("sdk", sdk_home) or "DFU")
    assert main(["flash", "romfs", str(root), "-b", "OPENMV4", "--sdk-home", "/opt/sdk"]) == 0
    assert str(seen["sdk"]) == "/opt/sdk"


def test_factory_coprocessor_flag(proj, capsys):
    root, _ran, artifact = proj
    artifact("OPENMV4-firmware.bin")
    artifact("OPENMV4-firmware-M55_HE.bin")
    artifact("OPENMV4-factory-romfs.img")
    # --coprocessor needs a coprocessor alt OPENMV4 doesn't have -> clean error, exit 2
    assert main(["flash", "factory", str(root), "-b", "OPENMV4", "--coprocessor"]) == 2
    assert "no 'coprocessor' flash target" in capsys.readouterr().err
