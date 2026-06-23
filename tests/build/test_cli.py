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
    assert "Built" in out and "OPENMV_N6.romfs" in out and "of ROMFS partition" in out


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


def test_build_romfs_ota_reports_trailer(make_project, capsys):
    files = {"main.py": "print(1)\n", "settings.json": '{"app_version": "1.0.0"}\n'}
    root, repo, app = make_project(ota=True, app_files=files)
    rc = main(["build", "romfs", str(root), "--app", str(app), "-f", str(repo),
               "--no-compile-py", "--no-convert-models"])
    assert rc == 0
    assert "signed trailer" in capsys.readouterr().out


def test_build_firmware_stub(capsys):
    assert main(["build", "firmware"]) == 2
    assert "not implemented" in capsys.readouterr().err


def _make_image_files(tmp_path):
    """A signed body + trailer + trusted_keys.json, built without a full project."""
    import hashlib

    from openmv_ota.ota import ES256, Trailer, algorithm_for, pack_trailer, signed_region
    from openmv_ota.ota.keys import (
        TrustedKey, generate_private_key, public_point_hex, write_trusted_keys)
    from openmv_ota.ota.sign import sign_region
    from openmv_ota.ota.version import encode_app_version

    spec = algorithm_for(ES256)
    priv = generate_private_key(spec)
    body = b"BODY" * 100
    t = Trailer(
        body_size=len(body), pad_size=16,
        meta={"product": "p", "board": "OPENMV_N6", "board_name": "P", "app_version": "1.2.3",
              "firmware": {"version": "5.0.0", "commit": "abc123def456"}, "micropython": "1.28.0",
              "toolchain": {"mpy_cross": "1.28.0", "vela": None, "stedgeai": None, "sdk": "1.6.0"}},
        board_id=7, min_platform_version=(5 << 24), payload_version=encode_app_version("1.2.3"),
        payload_version_floor=0, key_id=0x0100, sig_alg=ES256,
        body_sha256=hashlib.sha256(body).digest())
    t.signature = sign_region(priv, signed_region(t), spec)
    romfs = tmp_path / "x.romfs"
    romfs.write_bytes(body)
    trailer = tmp_path / "x.trailer"
    trailer.write_bytes(pack_trailer(t))
    keys = tmp_path / "trusted_keys.json"
    write_trusted_keys(keys, [TrustedKey(0x0100, ES256, "ota", public_point_hex(priv.public_key()))])
    return romfs, trailer, keys


def test_build_inspect(tmp_path, capsys):
    _romfs, trailer, _keys = _make_image_files(tmp_path)
    assert main(["build", "inspect", str(trailer)]) == 0
    out = capsys.readouterr().out
    assert "app_version" in out and "1.2.3" in out and "ES256" in out and "provenance" in out


def test_build_inspect_json(tmp_path, capsys):
    import json
    _romfs, trailer, _keys = _make_image_files(tmp_path)
    assert main(["build", "inspect", str(trailer), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["board_id"] == 7 and data["sig_alg"] == "ES256" and data["app_version"] == "1.2.3"


def test_build_inspect_bad_trailer(tmp_path, capsys):
    bad = tmp_path / "bad.trailer"
    bad.write_bytes(b"garbage-not-a-trailer")
    assert main(["build", "inspect", str(bad)]) == 2
    assert "not a valid trailer" in capsys.readouterr().err


def test_build_inspect_missing_file(tmp_path, capsys):
    assert main(["build", "inspect", str(tmp_path / "nope.trailer")]) == 2
    assert "error:" in capsys.readouterr().err


def test_build_verify_ok(tmp_path, capsys):
    romfs, trailer, keys = _make_image_files(tmp_path)
    assert main(["build", "verify", str(romfs), str(trailer), "--trusted-keys", str(keys)]) == 0
    assert "verified" in capsys.readouterr().out


def test_build_verify_fail(tmp_path, capsys):
    romfs, trailer, keys = _make_image_files(tmp_path)
    romfs.write_bytes(b"TAMPERED-BODY")  # body no longer matches the trailer
    assert main(["build", "verify", str(romfs), str(trailer), "--trusted-keys", str(keys)]) == 1
    assert "FAILED" in capsys.readouterr().err


def test_build_verify_missing_keys(tmp_path, capsys):
    romfs, trailer, _keys = _make_image_files(tmp_path)
    rc = main(["build", "verify", str(romfs), str(trailer),
               "--trusted-keys", str(tmp_path / "nope.json")])
    assert rc == 2 and "error:" in capsys.readouterr().err


def test_build_verify_missing_romfs(tmp_path, capsys):
    _romfs, trailer, keys = _make_image_files(tmp_path)
    rc = main(["build", "verify", str(tmp_path / "nope.romfs"), str(trailer),
               "--trusted-keys", str(keys)])
    assert rc == 2 and "error:" in capsys.readouterr().err
