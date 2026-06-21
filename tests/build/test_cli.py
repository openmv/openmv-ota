"""Tests for the ``openmv-ota build`` CLI."""

from __future__ import annotations

from openmv_ota.build import romfs as build_mod
from openmv_ota.cli import main


def test_build_romfs_pack_only(make_project, capsys):
    root, repo, app = make_project()
    rc = main(["build", "romfs", str(root), "--app", str(app), "-f", str(repo),
               "--no-compile-py", "--no-convert-models"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Built" in out and "OPENMV_N6.romfs" in out and "of partition" in out


def test_build_romfs_with_compile(monkeypatch, make_project):
    monkeypatch.setattr(build_mod.mpy, "compile_py", lambda mc, a, s, o: o.write_bytes(b"MPY"))
    monkeypatch.setattr(build_mod, "convert_model", lambda t, c, m: b"X")
    root, repo, app = make_project()
    rc = main(["build", "romfs", str(root), "--app", str(app), "-f", str(repo),
               "--vela-optimise", "Size", "--stedgeai-optimization", "1",
               "--mpy-arg=-O2", "--vela-arg=--foo", "--stedgeai-arg=--bar"])
    assert rc == 0


def test_build_romfs_error_exit_code(make_project, capsys):
    root, repo, app = make_project()
    rc = main(["build", "romfs", str(root), "--app", str(app), "-f", str(repo),
               "-b", "OPENMV_AE3", "--no-compile-py", "--no-convert-models"])
    assert rc == 2
    assert "no matching targets" in capsys.readouterr().err


def test_build_romfs_keep_build_dir(make_project, capsys):
    root, repo, app = make_project()
    rc = main(["build", "romfs", str(root), "--app", str(app), "-f", str(repo),
               "--no-compile-py", "--no-convert-models", "--keep-build-dir"])
    assert rc == 0
    assert "build dir kept" in capsys.readouterr().out


def test_build_firmware_stub(capsys):
    assert main(["build", "firmware"]) == 2
    assert "not implemented" in capsys.readouterr().err
