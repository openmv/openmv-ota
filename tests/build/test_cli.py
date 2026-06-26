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
    assert "Built" in out and "OPENMV_N6-romfs.img" in out and "of ROMFS partition" in out


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


def test_build_romfs_ota_reports_bundle(make_project, capsys):
    files = {"main.py": "print(1)\n", "settings.json": '{"app_version": "1.0.0"}\n'}
    root, repo, app = make_project(ota=True, app_files=files)
    rc = main(["build", "romfs", str(root), "--app", str(app), "-f", str(repo),
               "--no-compile-py", "--no-convert-models"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OPENMV_N6-romfs.zip" in out and "signed OTA bundle" in out


def test_build_ota_image_cli(make_project, capsys):
    root, repo, _ = make_project(boards=("OPENMV_N6",), ota=True)
    main(["build", "romfs", str(root), "-f", str(repo),
          "--no-compile-py", "--no-convert-models"])
    capsys.readouterr()
    rc = main(["build", "ota-image", str(root), "-f", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OPENMV_N6-ota.img.gz" in out and "OTA download image" in out


def test_build_ota_image_cli_error(make_project, capsys):
    root, repo, _ = make_project(boards=("OPENMV_N6",), ota=True)  # no bundle built
    rc = main(["build", "ota-image", str(root), "-f", str(repo)])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_build_manifest_cli(make_project, capsys):
    root, repo, _ = make_project(boards=("OPENMV_N6",), ota=True)
    main(["build", "romfs", str(root), "-f", str(repo),
          "--no-compile-py", "--no-convert-models"])
    main(["build", "ota-image", str(root), "-f", str(repo)])
    capsys.readouterr()
    rc = main(["build", "manifest", str(root), "-u", "https://dl.x.io/fw", "-f", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OPENMV_N6-manifest.bin" in out and "signed manifest" in out


def test_build_manifest_cli_error(make_project, capsys):
    root, repo, _ = make_project(boards=("OPENMV_N6",), ota=True)  # no ota-image built
    rc = main(["build", "manifest", str(root), "-u", "https://dl.x.io/fw", "-f", str(repo)])
    assert rc == 1
    assert "build ota-image" in capsys.readouterr().err


def test_build_ota_delta_cli(tmp_path, capsys):
    base = tmp_path / "b.img"
    target = tmp_path / "t.img"
    base_bytes = bytes(range(256)) * 100
    base.write_bytes(base_bytes)
    target.write_bytes(base_bytes[:3000] + b"NEW" * 50 + base_bytes[3000:])
    out = tmp_path / "d.delta.gz"
    rc = main(["build", "ota-delta", "--base", str(base), "--target", str(target),
               "-o", str(out)])
    assert rc == 0 and out.exists()
    assert "delta:" in capsys.readouterr().out


def test_build_ota_delta_cli_error(tmp_path, capsys, monkeypatch):
    base = tmp_path / "b.img"
    target = tmp_path / "t.img"
    base.write_bytes(b"A" * 4096)
    target.write_bytes(b"B" * 4096)
    from openmv_ota.ota import delta as delta_mod
    monkeypatch.setattr(delta_mod, "make_delta", lambda b, t: delta_mod.MAGIC + b"\x00")
    rc = main(["build", "ota-delta", "--base", str(base), "--target", str(target),
               "-o", str(tmp_path / "o.gz")])
    assert rc == 1 and "self-check" in capsys.readouterr().err


def _fake_firmware_make(monkeypatch):
    """Patch firmware._run_make to emit a stm32 firmware.bin on the build call."""
    from pathlib import Path

    from openmv_ota.build import firmware as fw

    def fake(repo, args):
        if "clean" in args:
            return
        target = next(a.split("=", 1)[1] for a in args if a.startswith("TARGET="))
        f = Path(repo) / "build" / target / "bin" / "firmware.bin"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"FW")

    monkeypatch.setattr(fw, "_run_make", fake)


def test_build_firmware_cli(make_project, monkeypatch, capsys):
    _fake_firmware_make(monkeypatch)
    root, repo, _app = make_project()
    assert main(["build", "firmware", str(root), "-f", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "OPENMV_N6-firmware.bin" in out and "firmware" in out


def test_build_firmware_cli_ota_keep(make_project, monkeypatch, capsys):
    _fake_firmware_make(monkeypatch)
    root, repo, _app = make_project(ota=True)
    assert main(["build", "firmware", str(root), "-f", str(repo), "--keep-build-dir"]) == 0
    out = capsys.readouterr().out
    assert "OTA firmware" in out and "wrapper dir kept" in out


def test_build_firmware_cli_error(make_project, capsys):
    root, repo, _app = make_project()
    rc = main(["build", "firmware", str(root), "-f", str(repo), "-b", "OPENMV_AE3"])
    assert rc != 0 and "no matching boards" in capsys.readouterr().err


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
    assert "not a valid" in capsys.readouterr().err


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


def _make_bundle(tmp_path):
    """A signed <board>-romfs.zip bundle + its trusted_keys.json."""
    from openmv_ota.ota import bundle
    romfs, trailer, keys = _make_image_files(tmp_path)
    z = tmp_path / "OPENMV_N6.zip"
    bundle.write_bundle(z, romfs.read_bytes(), trailer.read_bytes())
    return z, keys


def test_build_inspect_bundle(tmp_path, capsys):
    z, _keys = _make_bundle(tmp_path)
    assert main(["build", "inspect", str(z)]) == 0
    assert "ES256" in capsys.readouterr().out


def test_build_inspect_bad_bundle(tmp_path, capsys):
    import zipfile
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("hello.txt", "x")  # a zip, but not our bundle
    assert main(["build", "inspect", str(z)]) == 2
    assert "not an OTA bundle" in capsys.readouterr().err


def test_build_verify_bundle(tmp_path, capsys):
    z, keys = _make_bundle(tmp_path)
    assert main(["build", "verify", str(z), "--trusted-keys", str(keys)]) == 0
    assert "verified" in capsys.readouterr().out


def test_build_verify_single_arg_not_a_bundle(tmp_path, capsys):
    romfs, _trailer, keys = _make_image_files(tmp_path)  # a loose body, not a zip
    rc = main(["build", "verify", str(romfs), "--trusted-keys", str(keys)])
    assert rc == 2 and "not a .zip bundle" in capsys.readouterr().err


def test_build_factory_romfs(make_project, capsys):
    files = {"main.py": "print(1)\n", "settings.json": '{"app_version": "1.0.0"}\n'}
    root, repo, app = make_project(ota=True, app_files=files)
    rc = main(["build", "factory-romfs", str(root), "--app", str(app), "-f", str(repo),
               "--no-compile-py", "--no-convert-models", "--keep-build-dir"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OPENMV_N6-factory-romfs.img" in out and "factory image" in out
    assert "build dir kept" in out


def test_build_factory_romfs_non_ota_errors(make_project, capsys):
    root, repo, app = make_project()  # non-OTA
    rc = main(["build", "factory-romfs", str(root), "--app", str(app), "-f", str(repo),
               "--no-compile-py", "--no-convert-models"])
    assert rc != 0
    assert "needs an OTA project" in capsys.readouterr().err


# --- inspect / verify on a factory (dual-slot partition) image --------------

def _build_factory_image(make_project, capsys):
    """Build a real factory image; return (img_path, trusted_keys_path). Flushes the
    build's stdout from capsys so the caller reads only the inspect/verify output."""
    files = {"main.py": "print(1)\n", "settings.json": '{"app_version": "1.0.0"}\n'}
    root, repo, app = make_project(ota=True, app_files=files)
    assert main(["build", "factory-romfs", str(root), "--app", str(app), "-f", str(repo),
                 "--no-compile-py", "--no-convert-models"]) == 0
    capsys.readouterr()
    return root / "build" / "OPENMV_N6-factory-romfs.img", root / "keys" / "trusted_keys.json"


def test_build_inspect_factory(make_project, capsys):
    img, _keys = _build_factory_image(make_project, capsys)
    assert main(["build", "inspect", str(img)]) == 0
    out = capsys.readouterr().out
    assert "== FRONT slot ==" in out and "== BACK slot ==" in out and out.count("app_version") == 2


def test_build_inspect_factory_json(make_project, capsys):
    import json
    img, _keys = _build_factory_image(make_project, capsys)
    assert main(["build", "inspect", str(img), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"FRONT", "BACK"} and data["FRONT"]["app_version"] == "1.0.0"


def test_build_verify_factory_ok(make_project, capsys):
    img, keys = _build_factory_image(make_project, capsys)
    assert main(["build", "verify", str(img), "--trusted-keys", str(keys)]) == 0
    out = capsys.readouterr().out
    assert "FRONT: verified" in out and "BACK: verified" in out


def test_build_verify_factory_corrupted_slot(make_project, capsys):
    img, keys = _build_factory_image(make_project, capsys)
    data = bytearray(img.read_bytes())
    data[0] ^= 0xFF             # corrupt the FRONT slot's body (BACK stays intact)
    img.write_bytes(data)
    assert main(["build", "verify", str(img), "--trusted-keys", str(keys)]) == 1
    cap = capsys.readouterr()
    assert "FRONT: verification FAILED" in cap.err and "BACK: verified" in cap.out


# --- partition trailer scanning (unit) --------------------------------------

def test_find_trailers_skips_invalid_magic():
    from openmv_ota.ota import partition
    img = bytearray(8192)
    img[0:4] = b"OMVR"         # the magic, but not a valid trailer (zero header after)
    assert partition.find_trailers(bytes(img)) == []


def test_slots_empty_for_unsigned_image():
    from openmv_ota.ota import partition
    assert partition.slots(b"\x00" * 4096) == []


def test_slots_single_trailer(tmp_path):
    from openmv_ota.ota import partition
    romfs, trailer, _keys = _make_image_files(tmp_path)
    body, tb = romfs.read_bytes(), trailer.read_bytes()
    img = body + b"\xff" * (4096 - len(body)) + tb   # body in block 0, trailer at 4096
    sl = partition.slots(img)
    assert len(sl) == 1 and sl[0][0] == "image" and sl[0][1] == body


# --- a plain, unsigned romfs is handled gracefully (no trailer) --------------

def _plain_romfs(tmp_path):
    """A valid, unsigned ROMFS image (no OTA trailer), as `build romfs` makes for a
    non-OTA project or a coprocessor partition."""
    from openmv_ota.romfs.builder import build_image
    src = tmp_path / "rsrc"
    src.mkdir()
    (src / "main.py").write_text("print(1)\n")
    img = tmp_path / "plain-romfs.img"
    img.write_bytes(build_image(str(src)))
    return img


def test_build_inspect_unsigned_romfs(tmp_path, capsys):
    assert main(["build", "inspect", str(_plain_romfs(tmp_path))]) == 0
    assert "unsigned ROMFS image" in capsys.readouterr().out


def test_build_verify_unsigned_romfs(tmp_path, capsys):
    _r, _t, keys = _make_image_files(tmp_path)   # a valid trusted_keys.json
    img = _plain_romfs(tmp_path)
    assert main(["build", "verify", str(img), "--trusted-keys", str(keys)]) == 2
    assert "unsigned ROMFS image" in capsys.readouterr().err
