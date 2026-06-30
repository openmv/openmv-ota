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
    assert "would run: /x/dfu-util -w -d ,37c5:9204 -a 3" in capsys.readouterr().out


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
    assert main(["flash", "firmware", str(root), "-b", "MPS2_AN500"]) == 2
    assert "no flash configuration" in capsys.readouterr().err


def test_sdk_home_passed_through(proj, monkeypatch, capsys):
    root, ran, artifact = proj
    artifact("OPENMV4-romfs.img")
    seen = {}
    monkeypatch.setattr(fl.tools, "find_dfu_util",
                        lambda override, sdk_home: seen.setdefault("sdk", sdk_home) or "DFU")
    assert main(["flash", "romfs", str(root), "-b", "OPENMV4", "--sdk-home", "/opt/sdk"]) == 0
    assert str(seen["sdk"]) == "/opt/sdk"


def test_factory_error_returns_exit_code(proj, capsys):
    root, _ran, _artifact = proj
    assert main(["flash", "factory", str(root), "-b", "OPENMV4"]) == 2   # nothing built
    assert "error: missing artifact" in capsys.readouterr().err


def test_imx_rt1060_dry_run(proj, monkeypatch, capsys):
    root, ran, artifact = proj
    monkeypatch.setattr(fl.tools, "find_spsdk", lambda name, sdk_home: name)
    artifact("OPENMV_RT1060-firmware.bin")    # flashloaders are bundled in the package
    assert main(["flash", "firmware", str(root), "-b", "OPENMV_RT1060", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would run: sdphost -u 0x1FC9,0x0135 -- write-file" in out
    assert "would run: blhost -u 0x15A2,0x0073 -- reset" in out


def test_imx_rt1060_reports_step_labels(proj, monkeypatch, capsys):
    root, ran, artifact = proj
    monkeypatch.setattr(fl.tools, "find_spsdk", lambda name, sdk_home: name)
    monkeypatch.setattr(fl.time, "sleep", lambda _s: None)
    artifact("OPENMV_RT1060-firmware.bin")    # flashloaders are bundled in the package
    assert main(["flash", "firmware", str(root), "-b", "OPENMV_RT1060"]) == 0
    out = capsys.readouterr().out
    assert "reset (OPENMV_RT1060)" in out and "load flashloader" in out


def test_ae3_factory_flashes_all_partitions(proj, capsys):
    root, ran, artifact = proj
    for n in ("firmware-M55_HP.bin", "firmware-M55_HE.bin", "coprocessor-romfs.img",
              "factory-romfs.img"):
        artifact("OPENMV_AE3-%s" % n)
    assert main(["flash", "factory", str(root), "-b", "OPENMV_AE3", "--no-reset"]) == 0
    assert [a[5] for a in ran] == ["1", "2", "3", "6"]   # HP, HE, coproc romfs, main romfs
    assert all("--reset" not in a for a in ran)          # --no-reset honored on every step
