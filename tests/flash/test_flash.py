"""Orchestration: artifact selection, alt mapping, multi-step reset, dry-run, history."""

from __future__ import annotations

import pytest

from openmv_ota.flash import flash as fl
from openmv_ota.flash.errors import FlashError


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project dir with a build/ folder; the runner + tool + history are stubbed so the
    test asserts the argv sequence without touching hardware or the filesystem log."""
    (tmp_path / "build").mkdir()
    ran: list[list[str]] = []
    recorded: list[dict] = []
    monkeypatch.setattr(fl.runner, "run", lambda argv: ran.append(argv))
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: override or "DFU")
    monkeypatch.setattr(fl.history, "record",
                        lambda root, action, **f: recorded.append({"action": action, **f}))

    def artifact(name, data=b"x"):
        (tmp_path / "build" / name).write_bytes(data)

    return tmp_path, ran, recorded, artifact


def test_flash_firmware(project):
    root, ran, recorded, artifact = project
    artifact("OPENMV4-firmware.bin")
    steps = fl.flash_firmware(str(root), board="OPENMV4")
    assert [s.alt for s in steps] == [2]
    assert ran == [["DFU", "-w", "-d", ",37c5:9204", "-a", "2", "--reset",
                    "-D", str(root / "build/OPENMV4-firmware.bin")]]
    assert recorded == [{"action": "flash-firmware", "board": "OPENMV4",
                         "files": [{"file": "OPENMV4-firmware.bin", "alt": 2}]}]


def test_flash_factory_is_multistep_and_resets_only_last(project):
    root, ran, _rec, artifact = project
    artifact("OPENMV4-firmware.bin")
    artifact("OPENMV4-factory-romfs.img")
    steps = fl.flash_factory(str(root), board="OPENMV4")
    assert [(s.artifact, s.alt) for s in steps] == [("firmware", 2), ("romfs", 3)]
    assert "--reset" not in ran[0]                   # firmware step stays in the bootloader
    assert "--reset" in ran[1]                       # only the final write reboots


def test_flash_romfs(project):
    root, ran, _rec, artifact = project
    artifact("OPENMV4-romfs.img")
    fl.flash_romfs(str(root), board="OPENMV4")
    assert ran[0][4:6] == ["-a", "3"] and "--reset" in ran[0]


def test_no_reset(project):
    root, ran, _rec, artifact = project
    artifact("OPENMV4-romfs.img")
    fl.flash_romfs(str(root), board="OPENMV4", reset=False)
    assert "--reset" not in ran[0]


def test_ae3_firmware_uses_per_core_file_and_alt(project):
    root, ran, _rec, artifact = project
    artifact("OPENMV_AE3-firmware-M55_HP.bin")
    steps = fl.flash_firmware(str(root), board="OPENMV_AE3")
    assert steps[0].alt == 1 and steps[0].file.name == "OPENMV_AE3-firmware-M55_HP.bin"
    assert ran[0][3] == ",37c5:96e3"


def test_ae3_factory_coprocessor_flashes_all_four_partitions(project):
    root, ran, _rec, artifact = project
    for n in ("firmware-M55_HP", "firmware-M55_HE"):
        artifact("OPENMV_AE3-%s.bin" % n)
    artifact("OPENMV_AE3-coprocessor-romfs.img")
    artifact("OPENMV_AE3-factory-romfs.img")
    steps = fl.flash_factory(str(root), board="OPENMV_AE3", coprocessor=True)
    assert [(s.artifact, s.alt) for s in steps] == [
        ("firmware", 1), ("coprocessor", 2), ("coprocessor_romfs", 3), ("romfs", 6)]
    assert sum("--reset" in a for a in ran) == 1 and "--reset" in ran[-1]


def test_missing_artifact_fails_before_running(project):
    root, ran, _rec, _artifact = project
    with pytest.raises(FlashError, match="missing artifact"):
        fl.flash_firmware(str(root), board="OPENMV4")
    assert ran == []                                 # nothing flashed


def test_factory_resolves_all_before_flashing(project):
    # firmware present but the factory image isn't -> fail fast, flash nothing
    root, ran, _rec, artifact = project
    artifact("OPENMV4-firmware.bin")
    with pytest.raises(FlashError, match="factory-romfs"):
        fl.flash_factory(str(root), board="OPENMV4")
    assert ran == []


def test_dry_run_records_nothing_and_runs_nothing(project):
    root, ran, recorded, artifact = project
    artifact("OPENMV4-firmware.bin")
    steps = fl.flash_firmware(str(root), board="OPENMV4", dry_run=True)
    assert ran == [] and recorded == []
    assert "--reset" in steps[0].argv             # argv still built for display


def test_custom_output_dir(project, tmp_path):
    root, ran, _rec, _artifact = project
    out = tmp_path / "dist"
    out.mkdir()
    (out / "OPENMV4-romfs.img").write_bytes(b"x")
    fl.flash_romfs(str(root), board="OPENMV4", output=str(out))
    assert str(out / "OPENMV4-romfs.img") in ran[0]


def test_coprocessor_on_non_multicore_board_is_a_clean_error(project):
    # OPENMV4 has no coprocessor alt -> fail fast on the unsupported target (the flag exists;
    # the AE3 is the board it applies to).
    root, ran, _rec, artifact = project
    artifact("OPENMV4-firmware.bin")
    with pytest.raises(FlashError, match="no 'coprocessor' flash target"):
        fl.flash_firmware(str(root), board="OPENMV4", coprocessor=True)
    assert ran == []


def test_resolve_dfu_util_dry_run_tolerates_missing_dfu_util(monkeypatch):
    monkeypatch.setattr(fl.tools, "find_dfu_util",
                        lambda override, sdk_home: (_ for _ in ()).throw(FlashError("nope")))
    assert fl._resolve_dfu_util(None, None, dry_run=True) == "dfu-util"
    assert fl._resolve_dfu_util("/x/dfu-util", None, dry_run=True) == "/x/dfu-util"
    with pytest.raises(FlashError):
        fl._resolve_dfu_util(None, None, dry_run=False)


# --- imx (RT1060) -------------------------------------------------------------------------

@pytest.fixture
def imx_project(tmp_path, monkeypatch):
    """An RT1060 project with build artifacts + flashloaders; runner/tools/sleep stubbed."""
    (tmp_path / "build").mkdir()
    ran: list[list[str]] = []
    recorded: list[dict] = []
    monkeypatch.setattr(fl.runner, "run", lambda argv: ran.append(argv))
    monkeypatch.setattr(fl.tools, "find_spsdk", lambda name, sdk_home: name.upper())
    monkeypatch.setattr(fl.time, "sleep", lambda _s: None)
    monkeypatch.setattr(fl.history, "record",
                        lambda root, action, **f: recorded.append({"action": action, **f}))
    for n in ("OPENMV_RT1060-firmware.bin", "OPENMV_RT1060-romfs.img",
              "OPENMV_RT1060-factory-romfs.img", "sdphost_flash_loader.bin",
              "blhost_flash_loader.bin"):
        (tmp_path / "build" / n).write_bytes(b"x" * 5000)
    return tmp_path, ran, recorded


def test_imx_firmware_runs_the_sequence(imx_project):
    root, ran, recorded = imx_project
    steps = fl.flash_firmware(str(root), board="OPENMV_RT1060")
    assert ran[0][0] == "SDPHOST" and ran[-1][-1] == "reset"
    assert any("write-memory" in a and "0x60040000" in a for a in ran)
    assert recorded[0]["action"] == "flash-firmware" and recorded[0]["steps"] == \
        [s.label for s in steps]


def test_imx_factory_full_provision(imx_project):
    root, ran, _rec = imx_project
    fl.flash_factory(str(root), board="OPENMV_RT1060")
    flat = " ".join(" ".join(a) for a in ran)
    assert "efuse-program-once 0x06 00000010" in flat and "0x60001000" in flat


def test_imx_dry_run_runs_nothing(imx_project, monkeypatch):
    root, ran, recorded = imx_project
    slept = []
    monkeypatch.setattr(fl.time, "sleep", lambda s: slept.append(s))
    steps = fl.flash_romfs(str(root), board="OPENMV_RT1060", dry_run=True)
    assert ran == [] and recorded == [] and slept == []
    assert steps[-1].argv[-1] == "reset"


def test_imx_missing_flashloader_errors(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "OPENMV_RT1060-firmware.bin").write_bytes(b"x")
    monkeypatch.setattr(fl.tools, "find_spsdk", lambda name, sdk_home: name)
    with pytest.raises(FlashError, match="sdphost_flash_loader.bin"):
        fl.flash_firmware(str(tmp_path), board="OPENMV_RT1060")


def test_imx_flashloader_dir_override(imx_project, tmp_path):
    root, ran, _rec = imx_project
    loaders = tmp_path / "loaders"
    loaders.mkdir()
    (loaders / "sdphost_flash_loader.bin").write_bytes(b"x" * 100)
    # remove the in-build copy so only the override dir can satisfy it
    (root / "build" / "sdphost_flash_loader.bin").unlink()
    fl.flash_firmware(str(root), board="OPENMV_RT1060", flashloader_dir=str(loaders))
    assert str(loaders / "sdphost_flash_loader.bin") in ran[0]


def test_poll_retries_until_the_flashloader_answers(monkeypatch):
    calls = []

    def flaky(argv):
        calls.append(argv)
        if len(calls) < 3:                            # fail twice, then succeed
            raise FlashError("no device")

    monkeypatch.setattr(fl.runner, "run", flaky)
    monkeypatch.setattr(fl.time, "sleep", lambda _s: None)
    fl._poll(["blhost", "get-property", "1"])
    assert len(calls) == 3


def test_poll_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(fl.runner, "run",
                        lambda argv: (_ for _ in ()).throw(FlashError("no device")))
    monkeypatch.setattr(fl.time, "sleep", lambda _s: None)
    with pytest.raises(FlashError, match="never came up"):
        fl._poll(["blhost", "get-property", "1"])


def test_resolve_spsdk_dry_run_tolerates_missing(monkeypatch):
    monkeypatch.setattr(fl.tools, "find_spsdk",
                        lambda name, sdk_home: (_ for _ in ()).throw(FlashError("nope")))
    assert fl._resolve_spsdk("blhost", None, dry_run=True) == "blhost"
    with pytest.raises(FlashError):
        fl._resolve_spsdk("blhost", None, dry_run=False)
