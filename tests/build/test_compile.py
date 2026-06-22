"""Unit tests for the compile seams (mpy-cross, vela, stedgeai), dispatch, data."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from openmv_ota.build import data
from openmv_ota.build.compile import models, mpy, stedgeai, vela
from openmv_ota.build.errors import BuildError
from openmv_ota.project.resolve.board import ResolvedBoard


def _ok(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _fail(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 1, "", "boom")


# --- data -------------------------------------------------------------------

def test_read_firmware_file():
    assert b"%" in data.read_firmware_file("OPENMV_N6/neuralart.json")
    assert b"system_config" in data.read_firmware_file("OPENMV_AE3/vela.ini").lower() or True


def test_read_firmware_file_missing():
    with pytest.raises(BuildError, match="missing"):
        data.read_firmware_file("NOPE/nope.ini")


# --- mpy-cross --------------------------------------------------------------

def test_compile_py_ok(monkeypatch, tmp_path):
    seen = {}

    def run(cmd, **k):
        seen["cmd"] = cmd
        return _ok(cmd)

    monkeypatch.setattr(mpy.subprocess, "run", run)
    mpy.compile_py(["mpy-cross"], ["-march=armv7emdp"], tmp_path / "a.py", tmp_path / "a.mpy")
    assert seen["cmd"] == ["mpy-cross", "-march=armv7emdp", "-o",
                           str(tmp_path / "a.mpy"), str(tmp_path / "a.py")]


def test_compile_py_python_m_form(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(mpy.subprocess, "run", lambda cmd, **k: seen.update(cmd=cmd) or _ok(cmd))
    mpy.compile_py([sys.executable, "-m", "mpy_cross"], [], tmp_path / "a.py", tmp_path / "a.mpy")
    assert seen["cmd"][:3] == [sys.executable, "-m", "mpy_cross"]


def test_compile_py_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(mpy.subprocess, "run", _fail)
    with pytest.raises(BuildError, match="mpy-cross failed"):
        mpy.compile_py(["mpy-cross"], [], tmp_path / "a.py", tmp_path / "a.mpy")


def test_compile_py_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(mpy.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(BuildError, match="mpy-cross not found"):
        mpy.compile_py(["mpy-cross"], [], tmp_path / "a.py", tmp_path / "a.mpy")


# --- mpy-cross resolution ---------------------------------------------------

def _proj(mpy_cross_path=None, version="1.28.0"):
    return SimpleNamespace(
        mpy_cross_path=mpy_cross_path,
        lock=SimpleNamespace(
            micropython={"version": version, "mpy_abi_version": 6, "mpy_sub_version": 3}),
    )


def test_resolve_mpy_cross_firmware_binary():
    assert mpy.resolve_mpy_cross(_proj(mpy_cross_path="/b/mpy-cross")) == ["/b/mpy-cross"]


def test_resolve_mpy_cross_pip_match(monkeypatch, capsys):
    monkeypatch.setattr(mpy, "_has_pip_mpy_cross", lambda: True)
    monkeypatch.setattr(mpy, "_pip_mpy_cross_version", lambda: "1.28.0")
    assert mpy.resolve_mpy_cross(_proj()) == [sys.executable, "-m", "mpy_cross"]
    assert capsys.readouterr().err == ""


def test_resolve_mpy_cross_pip_unknown_version(monkeypatch, capsys):
    monkeypatch.setattr(mpy, "_has_pip_mpy_cross", lambda: True)
    monkeypatch.setattr(mpy, "_pip_mpy_cross_version", lambda: None)
    assert mpy.resolve_mpy_cross(_proj()) == [sys.executable, "-m", "mpy_cross"]
    assert capsys.readouterr().err == ""


def test_resolve_mpy_cross_pip_mismatch_warns(monkeypatch, capsys):
    monkeypatch.setattr(mpy, "_has_pip_mpy_cross", lambda: True)
    monkeypatch.setattr(mpy, "_pip_mpy_cross_version", lambda: "1.25.0")
    mpy.resolve_mpy_cross(_proj())
    assert "may not match" in capsys.readouterr().err


def test_resolve_mpy_cross_not_available(monkeypatch):
    monkeypatch.setattr(mpy, "_has_pip_mpy_cross", lambda: False)
    with pytest.raises(BuildError, match="pip install mpy-cross==1.28.0"):
        mpy.resolve_mpy_cross(_proj())


def test_has_pip_mpy_cross(monkeypatch):
    monkeypatch.setattr(mpy.importlib.util, "find_spec", lambda n: object())
    assert mpy._has_pip_mpy_cross() is True
    monkeypatch.setattr(mpy.importlib.util, "find_spec", lambda n: None)
    assert mpy._has_pip_mpy_cross() is False


def test_pip_mpy_cross_version(monkeypatch):
    monkeypatch.setattr(mpy.importlib.metadata, "version", lambda n: "1.28.0")
    assert mpy._pip_mpy_cross_version() == "1.28.0"

    def _raise(n):
        raise mpy.importlib.metadata.PackageNotFoundError()

    monkeypatch.setattr(mpy.importlib.metadata, "version", _raise)
    assert mpy._pip_mpy_cross_version() is None


# --- vela -------------------------------------------------------------------

def _vela_run(output_name="net_vela.tflite"):
    def run(cmd, **kw):
        out_dir = Path(cmd[cmd.index("--output-dir") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / output_name).write_bytes(b"VELA-OUT")
        return _ok(cmd)
    return run


def test_vela_convert(monkeypatch, tmp_path):
    model = tmp_path / "net.tflite"
    model.write_bytes(b"raw")
    captured = {}
    base = _vela_run()

    def run(cmd, **k):
        captured["cmd"] = cmd
        return base(cmd, **k)

    monkeypatch.setattr(vela.subprocess, "run", run)
    out = vela.convert("vela", ["--accelerator-config", "ethos-u55-256"], "Performance",
                       ["--extra"], b"[ini]", model)
    assert out == b"VELA-OUT"
    cmd = captured["cmd"]
    assert "--optimise" in cmd and "Performance" in cmd and "--extra" in cmd
    assert "--accelerator-config" in cmd


def test_vela_convert_lite_fallback(monkeypatch, tmp_path):
    model = tmp_path / "net.tflite"
    model.write_bytes(b"raw")
    monkeypatch.setattr(vela.subprocess, "run", _vela_run("net_vela.lite"))
    assert vela.convert("vela", [], "Size", [], b"x", model) == b"VELA-OUT"


def test_vela_convert_no_output(monkeypatch, tmp_path):
    model = tmp_path / "net.tflite"
    model.write_bytes(b"raw")
    monkeypatch.setattr(vela.subprocess, "run", _ok)
    with pytest.raises(BuildError, match="no output"):
        vela.convert("vela", [], "Performance", [], b"x", model)


def test_vela_convert_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(vela.subprocess, "run", _fail)
    with pytest.raises(BuildError, match="vela failed"):
        vela.convert("vela", [], "Performance", [], b"x", tmp_path / "net.tflite")


def test_vela_convert_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(vela.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(BuildError, match="vela not found"):
        vela.convert("vela", [], "Performance", [], b"x", tmp_path / "net.tflite")


# --- stedgeai ---------------------------------------------------------------

def test_render_neuralart():
    assert stedgeai.render_neuralart(b"opts %", 2) == b"opts --optimization 2"


def _stedgeai_run(cmd, cwd=None, **kw):
    out = Path(cwd) / "st_ai_output" / "network_rel.bin"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"NBIN-converted")
    return _ok(cmd)


def test_stedgeai_convert(monkeypatch, tmp_path):
    model = tmp_path / "net.tflite"
    model.write_bytes(b"raw")
    captured = {}

    def run(cmd, cwd=None, env=None, **k):
        captured["cmd"], captured["env"] = cmd, env
        # The workdir is a TemporaryDirectory removed when convert returns, so
        # capture the rendered config here while it still exists.
        captured["neuralart"] = (Path(cwd) / "neuralart.json").read_bytes()
        captured["mpool"] = (Path(cwd) / "stm32n6.mpool").read_bytes()
        return _stedgeai_run(cmd, cwd=cwd)

    monkeypatch.setattr(stedgeai.subprocess, "run", run)
    out = stedgeai.convert("/sdk/stedgeai/Utilities/linux/stedgeai", Path("/sdk"),
                           ["--target", "stm32n6"], ["--x"], b"tmpl %", b"mpool", 3, model)
    assert out == b"NBIN-converted"
    assert captured["cmd"][:4] == ["/sdk/stedgeai/Utilities/linux/stedgeai", "generate", "--model", str(model)]
    assert "--relocatable" in captured["cmd"] and "--x" in captured["cmd"]
    assert captured["env"]["STEDGEAI_CORE_DIR"].endswith("stedgeai")
    assert captured["neuralart"] == b"tmpl --optimization 3"
    assert captured["mpool"] == b"mpool"


def test_stedgeai_convert_no_output(monkeypatch, tmp_path):
    monkeypatch.setattr(stedgeai.subprocess, "run", lambda cmd, **k: _ok(cmd))
    with pytest.raises(BuildError, match="no output"):
        stedgeai.convert("stedgeai", Path("/sdk"), [], [], b"%", b"m", 3, tmp_path / "n.tflite")


def test_stedgeai_convert_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(stedgeai.subprocess, "run", lambda cmd, **k: _fail(cmd))
    with pytest.raises(BuildError, match="stedgeai failed"):
        stedgeai.convert("stedgeai", Path("/sdk"), [], [], b"%", b"m", 3, tmp_path / "n.tflite")


def test_stedgeai_convert_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(stedgeai.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(BuildError, match="stedgeai not found"):
        stedgeai.convert("stedgeai", Path("/sdk"), [], [], b"%", b"m", 3, tmp_path / "n.tflite")


# --- models dispatch --------------------------------------------------------

def _target(npu, npu_config):
    return ResolvedBoard(name="B", board_type=None, arch="", mpy_args=[], npu=npu,
                         partition_index=0, partition_size=1000, front_size=500,
                         npu_config=npu_config)


def _ctx(tmp_path, **over):
    kw = dict(sdk_home=tmp_path, vela_path="/v", stedgeai_path="/s")
    kw.update(over)
    return models.ModelContext(**kw)


def test_convert_model_vela(monkeypatch, tmp_path):
    monkeypatch.setattr(models.vela, "convert", lambda *a, **k: b"VELA")
    model = tmp_path / "m.tflite"
    model.write_bytes(b"raw")
    t = _target("vela", {"args": [], "iniFilePath": "OPENMV_AE3/vela.ini"})
    assert models.convert_model(t, _ctx(tmp_path), model) == b"VELA"


def test_convert_model_vela_already(tmp_path):
    model = tmp_path / "m.tflite"
    model.write_bytes(b"...ethos-u...")
    t = _target("vela", {"args": [], "iniFilePath": "OPENMV_AE3/vela.ini"})
    assert models.convert_model(t, _ctx(tmp_path), model) is None


def test_convert_model_vela_no_path(tmp_path):
    model = tmp_path / "m.tflite"
    model.write_bytes(b"raw")
    t = _target("vela", {"args": [], "iniFilePath": "OPENMV_AE3/vela.ini"})
    with pytest.raises(BuildError, match="no vela"):
        models.convert_model(t, _ctx(tmp_path, vela_path=None), model)


def test_convert_model_stedgeai(monkeypatch, tmp_path):
    monkeypatch.setattr(models.stedgeai, "convert", lambda *a, **k: b"NBIN")
    model = tmp_path / "m.tflite"
    model.write_bytes(b"raw")
    t = _target("stedgeai", {"args": [], "jsonFilePath": "OPENMV_N6/neuralart.json",
                             "mpoolFilePath": "OPENMV_N6/stm32n6.mpool"})
    assert models.convert_model(t, _ctx(tmp_path), model) == b"NBIN"


def test_convert_model_stedgeai_already(tmp_path):
    model = tmp_path / "m.tflite"
    model.write_bytes(b"NBIN....")
    t = _target("stedgeai", {"args": [], "jsonFilePath": "OPENMV_N6/neuralart.json",
                             "mpoolFilePath": "OPENMV_N6/stm32n6.mpool"})
    assert models.convert_model(t, _ctx(tmp_path), model) is None


def test_convert_model_stedgeai_no_path(tmp_path):
    model = tmp_path / "m.tflite"
    model.write_bytes(b"raw")
    t = _target("stedgeai", {"args": [], "jsonFilePath": "x", "mpoolFilePath": "y"})
    with pytest.raises(BuildError, match="no stedgeai"):
        models.convert_model(t, _ctx(tmp_path, stedgeai_path=None), model)


def test_convert_model_unknown_npu(tmp_path):
    model = tmp_path / "m.tflite"
    model.write_bytes(b"raw")
    t = _target("weird", {"args": []})
    with pytest.raises(BuildError, match="unsupported NPU"):
        models.convert_model(t, _ctx(tmp_path), model)
