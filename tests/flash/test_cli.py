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
    monkeypatch.setattr(fl, "_imx_catch_and_reset", lambda *a, **k: None)   # imx hardware step; stub
    monkeypatch.setattr(fl.history, "record", lambda *a, **k: None)
    monkeypatch.setattr(fl, "provision_keys", lambda **k: [])   # its own tests: test_identity

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
    assert main(["flash", "factory", str(root), "-b", "OPENMV4", "--no-register"]) == 0
    assert len(ran) == 2


def test_flash_romfs_dry_run_prints_command(proj, capsys):
    root, ran, artifact = proj
    artifact("OPENMV4-romfs.img")
    assert main(["flash", "romfs", str(root), "-b", "OPENMV4", "--dry-run",
                 "--dfu-util", "/x/dfu-util"]) == 0
    assert ran == []
    assert "would run: /x/dfu-util -d ,37c5:9204 -a 3" in capsys.readouterr().out


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
    assert main(["flash", "factory", str(root), "-b", "OPENMV4", "--no-register"]) == 2   # nothing built
    assert "error: missing artifact" in capsys.readouterr().err


def test_imx_rt1060_dry_run(proj, monkeypatch, capsys):
    root, ran, artifact = proj
    monkeypatch.setattr(fl.tools, "find_spsdk", lambda name, sdk_home: name)
    artifact("OPENMV_RT1060-firmware.bin")    # flashloaders are bundled in the package
    artifact("OPENMV_RT1060-stub.bin")        # a stub board: the composed image is what is written
    assert main(["flash", "firmware", str(root), "-b", "OPENMV_RT1060", "--dry-run"]) == 0
    out = capsys.readouterr().out
    # resident-SBL path: no sdphost, no FlexSPI config-register writes -- erase+write firmware, reset
    assert "sdphost" not in out and "fill-memory" not in out
    assert "would run: blhost -u 0x15A2,0x0073 -t 120000 -- flash-erase-region 0x60040000" in out
    assert "would run: blhost -u 0x15A2,0x0073 -- reset" in out


def test_imx_rt1060_reports_step_labels(proj, monkeypatch, capsys):
    root, ran, artifact = proj
    monkeypatch.setattr(fl.tools, "find_spsdk", lambda name, sdk_home: name)
    artifact("OPENMV_RT1060-firmware.bin")    # flashloaders are bundled in the package
    artifact("OPENMV_RT1060-stub.bin")
    assert main(["flash", "firmware", str(root), "-b", "OPENMV_RT1060"]) == 0
    out = capsys.readouterr().out
    assert "reset (OPENMV_RT1060)" in out and "write" in out and "0x60040000" in out
    assert "OPENMV_RT1060-firmware-with-stub.bin" in out


def test_erase_romfs_dry_run(proj, monkeypatch, capsys):
    root, ran, artifact = proj
    monkeypatch.setattr(fl.tools, "find_spsdk", lambda name, sdk_home: name)
    assert main(["flash", "erase", str(root), "-b", "OPENMV_RT1060", "--romfs", "--dry-run"]) == 0
    out = capsys.readouterr().out
    # 8 MB of NOR at the SBL's ~50 KB/s outlasts the 120 s floor: the timeout scales with the region
    assert "would run: blhost -u 0x15A2,0x0073 -t 512000 -- flash-erase-region 0x60800000 0x800000" in out
    assert "would run: blhost -u 0x15A2,0x0073 -- reset" in out


def test_read_dry_run_and_its_refusal_off_imx(proj, monkeypatch, capsys):
    root, ran, artifact = proj
    monkeypatch.setattr(fl.tools, "find_spsdk", lambda name, sdk_home: name)
    assert main(["flash", "read", str(root), "-b", "OPENMV_RT1060", "--part", "romfs",
                 "--to", "slots.bin", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would run: blhost -u 0x15A2,0x0073 -t 600000 -- read-memory 0x60800000 0x800000 slots.bin" in out
    assert "would run: blhost -u 0x15A2,0x0073 -- reset" in out
    assert main(["flash", "read", str(root), "-b", "OPENMV_N6", "--part", "firmware",
                 "--to", "fw.bin", "--dry-run"]) != 0
    assert "only supported on the imx backend" in capsys.readouterr().err


def test_arduino_factory_dry_run(proj, capsys):
    root, ran, artifact = proj
    for n in ("firmware.bin", "factory-romfs.img", "stub.bin", "firmware-text2.bin"):
        artifact("ARDUINO_PORTENTA_H7-%s" % n)
    for n in ("cyw4343_7_45_98_102.bin", "cyw4343_btfw.bin"):   # build emits these to out_dir
        artifact(n)
    assert main(["flash", "factory", str(root), "-b", "ARDUINO_PORTENTA_H7", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would run: DFU -w -d ,2341:035b -a 0 -s 0x08020000 " in out   # the second text region
    assert "would run: DFU -w -d ,2341:035b -a 0 -s 0x08040000 " in out   # a later step: keeps -w
    assert "ARDUINO_PORTENTA_H7-firmware-with-stub.bin" in out              # the composed image, not the bare firmware
    assert "would run: DFU -d ,2341:035b -a 1 -s 0x90F00000 " in out      # the first: no -w
    assert "0x90B00000:leave" in out


def test_flash_in_bootloader_skips_reset(proj, monkeypatch, capsys):
    root, ran, artifact = proj
    monkeypatch.setattr(fl.device, "reset", lambda *a, **k: pytest.fail("should not reset"))
    artifact("ARDUINO_PORTENTA_H7-firmware.bin")
    artifact("ARDUINO_PORTENTA_H7-stub.bin")
    (root / "build" / "ARDUINO_PORTENTA_H7-firmware-text2.bin").write_bytes(b"")   # nothing moved: no extra write
    assert main(["flash", "firmware", str(root), "-b", "ARDUINO_PORTENTA_H7",
                 "--in-bootloader"]) == 0
    assert len(ran) == 1
    assert "firmware -> 0x08040000:leave (ARDUINO_PORTENTA_H7)" in capsys.readouterr().out


def test_ae3_factory_flashes_all_partitions(proj, capsys):
    root, ran, artifact = proj
    for n in ("stub.bin", "firmware-M55_HP.bin", "firmware-M55_HE.bin", "coprocessor-romfs.img",
              "factory-romfs.img"):
        artifact("OPENMV_AE3-%s" % n)
    assert main(["flash", "factory", str(root), "-b", "OPENMV_AE3", "--no-reset",
                 "--no-register"]) == 0
    assert [a[a.index("-a") + 1] for a in ran] == ["1", "2", "3", "6"]   # HP, HE, coproc + main romfs
    assert all("--reset" not in a for a in ran)          # --no-reset honored on every step


def test_flash_register_claims_a_flashed_camera(proj, monkeypatch, capsys):
    from openmv_ota.flash import cli as flash_cli
    root, ran, _artifact = proj
    got = {}

    class _Desk:
        def __init__(self, args):
            got["project"] = args.project

        def check(self):
            got["checked"] = True

    monkeypatch.setattr(flash_cli, "_Registrar", _Desk)
    monkeypatch.setattr(fl, "provision_keys", lambda **k: got.update(k) or [])
    assert main(["flash", "register", str(root), "-b", "OPENMV4", "--serial", "/dev/x"]) == 0
    assert got["checked"] and got["board"] == "OPENMV4" and got["serial"] == "/dev/x"
    assert got["running"] is True                                # reset before asking
    assert isinstance(got["registrar"], _Desk) and ran == []      # nothing flashed


def test_flash_register_needs_a_desk(proj, capsys):
    root, ran, _artifact = proj
    assert main(["flash", "register", str(root), "-b", "OPENMV4"]) != 0   # no project here
    assert "can't register cameras from this desk" in capsys.readouterr().err and ran == []


def test_a_saved_token_is_not_sent_to_the_server_a_project_names(tmp_path, monkeypatch):
    """The project may be one someone else made, and its config names a server. The token
    is the user's, saved for theirs."""
    from types import SimpleNamespace

    from openmv_ota.client import config as client_config
    from openmv_ota.flash import cli as flash_cli
    from openmv_ota.flash.errors import FlashError
    from openmv_ota.project import config as project_config
    from openmv_ota.project import desk_keys
    named = {"url": "https://theirs"}
    monkeypatch.setattr(project_config, "load_config",
                        lambda path: SimpleNamespace(account_id="a", server_url=named["url"]))
    monkeypatch.setattr(desk_keys, "current", lambda root, desk: object())
    monkeypatch.setattr(client_config, "load", lambda path=None: client_config.ClientConfig(
        server_url="https://mine", token="saved"))
    monkeypatch.delenv("OPENMV_OTA_SERVER", raising=False)
    monkeypatch.delenv("OPENMV_OTA_TOKEN", raising=False)

    def check(**flags):
        desk = flash_cli._Registrar(SimpleNamespace(**{
            "project": str(tmp_path), "desk": None, "server": None, "token": None, **flags}))
        desk.check()
        return desk.client_cfg

    with pytest.raises(FlashError, match="saved token is for https://mine"):
        check()
    with pytest.raises(FlashError, match="saved token is for https://mine"):
        check(token="saved")                                               # the same one, typed
    assert check(server="https://theirs").server_url == "https://theirs"   # said on purpose
    assert check(token="other").token == "other"                           # given for this run
    named["url"] = "https://mine/"
    assert check().token == "saved"
    named["url"] = ""
    assert check().server_url == "https://mine"
