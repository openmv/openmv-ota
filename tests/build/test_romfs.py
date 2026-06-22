"""Tests for the build_romfs orchestration."""

from __future__ import annotations

from openmv_ota.build import romfs as build_mod
from openmv_ota.build.errors import BuildError
from openmv_ota.romfs.builder import read_image

import pytest


def _names(image_path):
    return {p for p, e in read_image(image_path.read_bytes()).walk()}


def _fake_compile(mpy_cross, args, src, out):
    out.write_bytes(b"MPY:" + src.read_bytes())


def test_pack_only(make_project):
    root, repo, app = make_project()
    results = build_mod.build_romfs(root, app=app, firmware=repo,
                                    compile_py=False, convert_models=False)
    assert len(results) == 1
    names = _names(results[0].output)
    assert "main.py" in names and "lib/util.py" in names and "net.tflite" in names


def test_compiles_all_py(monkeypatch, make_project):
    monkeypatch.setattr(build_mod.mpy, "compile_py", _fake_compile)
    root, repo, app = make_project()
    results = build_mod.build_romfs(root, app=app, firmware=repo, convert_models=False)
    names = _names(results[0].output)
    assert "main.mpy" in names and "lib/util.mpy" in names  # main.py compiled too
    assert "main.py" not in names and "lib/util.py" not in names


def test_converts_models(monkeypatch, make_project):
    monkeypatch.setattr(build_mod.mpy, "compile_py", _fake_compile)
    monkeypatch.setattr(build_mod, "convert_model", lambda t, ctx, m: b"CONVERTED")
    root, repo, app = make_project()
    results = build_mod.build_romfs(root, app=app, firmware=repo)
    reader = read_image(results[0].output.read_bytes())
    net = next(e for p, e in reader.walk() if p == "net.tflite")
    assert net.data == b"CONVERTED"


def test_skip_detection_leaves_model(monkeypatch, make_project):
    # convert_model returns None (already converted) -> file packed unchanged.
    monkeypatch.setattr(build_mod, "convert_model", lambda t, ctx, m: None)
    root, repo, app = make_project(app_files={"net.tflite": b"ALREADYCONVERTED"})
    results = build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False)
    reader = read_image(results[0].output.read_bytes())
    net = next(e for p, e in reader.walk() if p == "net.tflite")
    assert net.data == b"ALREADYCONVERTED"


def test_no_npu_board_skips_conversion(monkeypatch, make_project):
    called = {"n": 0}
    monkeypatch.setattr(build_mod, "convert_model", lambda *a: called.__setitem__("n", called["n"] + 1))
    root, repo, app = make_project(boards=("ARDUINO_NICLA_VISION",))
    build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False)
    assert called["n"] == 0  # NiclaV has no NPU


def test_mpy_cross_not_available(make_project, monkeypatch):
    # No firmware-built binary and no pip mpy_cross -> error with the pip hint.
    monkeypatch.setattr(build_mod.mpy, "_has_pip_mpy_cross", lambda: False)
    root, repo, app = make_project(with_mpy_cross=False)
    with pytest.raises(BuildError, match="pip install mpy-cross"):
        build_mod.build_romfs(root, app=app, firmware=repo, convert_models=False)


def test_mpy_cross_via_pip(monkeypatch, make_project):
    # No firmware binary, but pip mpy_cross is available -> uses python -m mpy_cross.
    monkeypatch.setattr(build_mod.mpy, "_has_pip_mpy_cross", lambda: True)
    monkeypatch.setattr(build_mod.mpy, "_pip_mpy_cross_version", lambda: "1.28.0")
    seen = {}
    monkeypatch.setattr(build_mod.mpy, "compile_py",
                        lambda cmd, a, s, o: seen.update(cmd=cmd) or o.write_bytes(b"MPY"))
    root, repo, app = make_project(with_mpy_cross=False)
    build_mod.build_romfs(root, app=app, firmware=repo, convert_models=False)
    assert seen["cmd"][1:] == ["-m", "mpy_cross"]


def test_missing_app(make_project, tmp_path):
    root, repo, _ = make_project()
    with pytest.raises(BuildError, match="app directory not found"):
        build_mod.build_romfs(root, app=tmp_path / "nope", firmware=repo,
                              compile_py=False, convert_models=False)


def test_no_matching_targets(make_project):
    root, repo, app = make_project()
    with pytest.raises(BuildError, match="no matching targets"):
        build_mod.build_romfs(root, app=app, firmware=repo, boards=["OPENMV_AE3"],
                              compile_py=False, convert_models=False)


def test_oversize(monkeypatch, make_project):
    monkeypatch.setattr(build_mod, "build_image", lambda *a, **k: b"\x00" * (30 * 1024 * 1024))
    root, repo, app = make_project()
    with pytest.raises(BuildError, match="over"):
        build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)
    # allow_oversize writes it anyway
    res = build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False,
                                convert_models=False, allow_oversize=True)
    assert res[0].output.exists()


def test_ota_capacity_is_half(make_project):
    from openmv_ota.project import load_project

    root, repo, app = make_project(ota=True)
    target = load_project(root, firmware=repo).board("OPENMV_N6")
    results = build_mod.build_romfs(root, app=app, firmware=repo,
                                    compile_py=False, convert_models=False)
    r = results[0]
    assert r.bound == "OTA slot"
    assert r.capacity == target.front_size - build_mod.OTA_SLOT_OVERHEAD
    assert r.capacity < target.partition_size


def test_ota_image_over_slot_budget(monkeypatch, make_project):
    from openmv_ota.project import load_project

    root, repo, app = make_project(ota=True)
    front = load_project(root, firmware=repo).board("OPENMV_N6").front_size
    # Fits the whole partition, but overflows a half-partition OTA slot.
    monkeypatch.setattr(build_mod, "build_image", lambda *a, **k: b"\x00" * (front - 0x1000))
    with pytest.raises(BuildError, match="OTA slot"):
        build_mod.build_romfs(root, app=app, firmware=repo,
                              compile_py=False, convert_models=False)


def test_multi_partition_naming(make_project):
    root, repo, app = make_project(
        boards=("OPENMV_AE3",),
        extra_config="\n[targets.OPENMV_AE3]\npartitions = [0, 1]\n",
    )
    results = build_mod.build_romfs(root, app=app, firmware=repo,
                                    compile_py=False, convert_models=False)
    outs = {r.output.name for r in results}
    assert outs == {"OPENMV_AE3-p0.romfs", "OPENMV_AE3-p1.romfs"}


def test_board_partition_filters(make_project):
    root, repo, app = make_project(
        boards=("OPENMV_AE3",),
        extra_config="\n[targets.OPENMV_AE3]\npartitions = [0, 1]\n",
    )
    results = build_mod.build_romfs(root, app=app, firmware=repo, partition=1,
                                    compile_py=False, convert_models=False)
    assert len(results) == 1 and results[0].partition_index == 1


def test_keep_build_dir(make_project):
    root, repo, app = make_project()
    results = build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False,
                                    convert_models=False, keep_build_dir=True)
    assert results[0].build_dir is not None and results[0].build_dir.exists()


def test_default_app_and_output_dirs(monkeypatch, make_project):
    # No --app/--output: defaults to <project>/app and <project>/build.
    root, repo, app = make_project()
    import shutil
    shutil.copytree(app, root / "app")
    results = build_mod.build_romfs(root, firmware=repo, compile_py=False, convert_models=False)
    assert results[0].output == root / "build" / "OPENMV_N6.romfs"


def test_drift_refuses(make_project, git_cmd):
    root, repo, app = make_project()
    (repo / "newfile.txt").write_text("x")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "drift")
    with pytest.raises(BuildError, match="refusing to proceed"):
        build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)
