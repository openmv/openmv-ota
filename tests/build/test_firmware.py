"""Tests for ``openmv-ota build firmware`` (the make invocation is mocked -- no
ARM toolchain in CI)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openmv_ota.build import firmware as fw
from openmv_ota.build.errors import BuildError


def _fake_make(artifacts):
    """A drop-in for ``fw._run_make`` that records calls and, on the build call
    (not ``clean``), drops ``artifacts`` (paths relative to ``build/<TARGET>``)."""
    calls: list[list[str]] = []

    def fake(repo, args):
        calls.append(list(args))
        target = next(a.split("=", 1)[1] for a in args if a.startswith("TARGET="))
        if "clean" in args:
            return
        bdir = Path(repo) / "build" / target
        for rel in artifacts:
            f = bdir / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"FW")

    fake.calls = calls
    return fake


def test_build_firmware_non_ota(make_project, monkeypatch):
    fake = _fake_make(["bin/firmware.bin"])
    monkeypatch.setattr(fw, "_run_make", fake)
    root, repo, _app = make_project()
    results = fw.build_firmware(root, firmware=repo)
    assert len(results) == 1
    r = results[0]
    assert r.board == "OPENMV_N6" and r.ota is False and r.build_dir is None
    assert [o.name for o in r.outputs] == ["OPENMV_N6.bin"]
    assert r.outputs[0].read_bytes() == b"FW"
    # clean then build (default), build carries the TARGET + -j
    assert fake.calls[0] == ["TARGET=OPENMV_N6", "clean"]
    assert "TARGET=OPENMV_N6" in fake.calls[1] and any(a.startswith("-j") for a in fake.calls[1])
    assert not any(a.startswith("FROZEN_MANIFEST=") for a in fake.calls[1])


def test_build_firmware_incremental_skips_clean(make_project, monkeypatch):
    fake = _fake_make(["bin/firmware.bin"])
    monkeypatch.setattr(fw, "_run_make", fake)
    root, repo, _app = make_project()
    fw.build_firmware(root, firmware=repo, incremental=True, jobs=4)
    assert len(fake.calls) == 1                       # no clean
    assert "-j4" in fake.calls[0]


def test_build_firmware_ignores_openmv_bin(make_project, monkeypatch):
    # The bootloader-combined openmv.bin is deliberately not collected; only
    # firmware.bin is. firmware.bin present -> openmv.bin alongside is ignored.
    fake = _fake_make(["bin/firmware.bin", "bin/openmv.bin"])
    monkeypatch.setattr(fw, "_run_make", fake)
    root, repo, _app = make_project()
    r = fw.build_firmware(root, firmware=repo)[0]
    assert [o.name for o in r.outputs] == ["OPENMV_N6.bin"]


def test_build_firmware_openmv_bin_only_is_no_image(make_project, monkeypatch):
    # openmv.bin without a firmware.bin counts as no firmware image at all.
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/openmv.bin"]))
    root, repo, _app = make_project()
    with pytest.raises(BuildError, match="produced no image"):
        fw.build_firmware(root, firmware=repo)


def test_build_firmware_alif_per_core(make_project, monkeypatch):
    fake = _fake_make(["bin/firmware_M55_HP.bin", "bin/firmware_M55_HE.bin", "bin/firmware.toc"])
    monkeypatch.setattr(fw, "_run_make", fake)
    root, repo, _app = make_project(boards=("OPENMV_AE3",))
    r = fw.build_firmware(root, firmware=repo)[0]
    # both cores collected, the bootloader-written .toc ignored
    assert sorted(o.name for o in r.outputs) == ["OPENMV_AE3-M55_HE.bin", "OPENMV_AE3-M55_HP.bin"]


def test_build_firmware_ota_injects_boot(make_project, monkeypatch):
    fake = _fake_make(["bin/firmware.bin"])
    monkeypatch.setattr(fw, "_run_make", fake)
    root, repo, _app = make_project(ota=True)
    r = fw.build_firmware(root, firmware=repo, keep_build_dir=True)
    r = r[0]
    assert r.ota is True and r.build_dir is not None
    # FROZEN_MANIFEST was pointed at our wrapper
    frozen = [a for a in fake.calls[1] if a.startswith("FROZEN_MANIFEST=")]
    assert len(frozen) == 1 and frozen[0].endswith("manifest.py")
    # wrapper includes the board manifest + freezes boot.py; boot.py exists
    manifest = (r.build_dir / "manifest.py").read_text()
    assert "include(" in manifest and "boards/OPENMV_N6/manifest.py" in manifest
    assert 'freeze(' in manifest and "boot.py" in manifest
    assert (r.build_dir / "boot.py").exists()


def test_build_firmware_ota_cleans_wrapper(make_project, monkeypatch):
    captured = {}
    real_writer = fw._write_wrapper_manifest

    def spy(repo, name):
        d = real_writer(repo, name)
        captured["dir"] = d
        return d

    monkeypatch.setattr(fw, "_write_wrapper_manifest", spy)
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project(ota=True)
    fw.build_firmware(root, firmware=repo)  # no keep -> wrapper dir removed
    assert not captured["dir"].exists()


def test_build_firmware_no_image_errors(make_project, monkeypatch):
    monkeypatch.setattr(fw, "_run_make", _fake_make([]))  # build produces nothing
    root, repo, _app = make_project()
    with pytest.raises(BuildError, match="produced no image"):
        fw.build_firmware(root, firmware=repo)


def test_build_firmware_no_matching_boards(make_project, monkeypatch):
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project()
    with pytest.raises(BuildError, match="no matching boards"):
        fw.build_firmware(root, firmware=repo, boards=["OPENMV_AE3"])


def test_build_firmware_refuses_on_drift(make_project, git_cmd, monkeypatch):
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project()
    (repo / "newfile.txt").write_text("x")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "drift")
    with pytest.raises(BuildError, match="refusing to proceed"):
        fw.build_firmware(root, firmware=repo)


def test_run_make_success(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    fw._run_make(tmp_path, ["TARGET=X", "clean"])
    assert seen["cmd"] == ["make", "TARGET=X", "clean"] and seen["kw"]["check"] is True


def test_run_make_not_found(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(BuildError, match="make not found"):
        fw._run_make(tmp_path, ["TARGET=X"])


def test_run_make_failed(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(2, ["make"])

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(BuildError, match="firmware build failed"):
        fw._run_make(tmp_path, ["TARGET=X"])
