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


def test_resolve_tool_dry_run_tolerates_missing_dfu_util(monkeypatch):
    monkeypatch.setattr(fl.tools, "find_dfu_util",
                        lambda override, sdk_home: (_ for _ in ()).throw(FlashError("nope")))
    assert fl._resolve_tool(None, None, dry_run=True) == "dfu-util"
    assert fl._resolve_tool("/x/dfu-util", None, dry_run=True) == "/x/dfu-util"
    with pytest.raises(FlashError):
        fl._resolve_tool(None, None, dry_run=False)
