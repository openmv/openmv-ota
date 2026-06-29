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


def test_build_ota_romfs_cli_relative_urls(make_project, capsys):
    # one shot from app source (no separate `build romfs`); relative filename URLs
    from openmv_ota.ota.manifest import parse_manifest
    root, repo, _ = make_project(boards=("OPENMV_N6",), ota=True)
    rc = main(["build", "ota-romfs", str(root), "-f", str(repo),
               "--no-compile-py", "--no-convert-models"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OPENMV_N6-ota.img.gz" in out and "OPENMV_N6-manifest.bin" in out
    body = parse_manifest((root / "build" / "OPENMV_N6-manifest.bin").read_bytes()).body
    assert body["representations"][0]["url"] == "OPENMV_N6-ota.img.gz"   # relative


def test_build_ota_romfs_cli_with_delta(make_project, capsys):
    # --delta-from a factory image -> a delta representation, base read from BACK trailer
    from openmv_ota.ota.manifest import parse_manifest
    root, repo, _ = make_project(boards=("OPENMV_N6",), ota=True)
    build_mod.build_factory_romfs(root, firmware=repo, compile_py=False, convert_models=False)
    capsys.readouterr()
    factory = root / "build" / "OPENMV_N6-factory-romfs.img"
    rc = main(["build", "ota-romfs", str(root), "-f", str(repo), "--delta-from", str(factory),
               "--no-compile-py", "--no-convert-models"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OPENMV_N6-ota.delta.gz" in out
    body = parse_manifest((root / "build" / "OPENMV_N6-manifest.bin").read_bytes()).body
    fmts = {r["format"] for r in body["representations"]}
    assert fmts == {"full", "ocdl"}


def test_build_ota_romfs_cli_error(make_project, capsys):
    root, repo, _ = make_project(boards=("OPENMV_N6",))    # not an OTA project
    rc = main(["build", "ota-romfs", str(root), "-f", str(repo),
               "--no-compile-py", "--no-convert-models"])
    assert rc != 0
    assert "OTA" in capsys.readouterr().err


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


# --- inspect / verify of manifests + deltas ----------------------------------

def _write_manifest_and_keys(d, *, key_id=0x0100, sign_key_id=None, with_delta=True):
    """Write a signed <d>/m.bin + a <d>/trusted_keys.json that trusts it. Returns nothing;
    `sign_key_id` (default key_id) signs, so passing a different key_id forges a bad sig."""
    import gzip

    from openmv_ota.ota import ES256, algorithm_for
    from openmv_ota.ota.delta import make_delta
    from openmv_ota.ota.keys import (
        TrustedKey, generate_private_key, public_point_hex, write_trusted_keys)
    from openmv_ota.ota.manifest import Manifest, pack_manifest, signed_region
    from openmv_ota.ota.sign import sign_region

    spec = algorithm_for(ES256)
    priv = generate_private_key(spec)
    reps = [{"format": "full", "url": "https://x/n6.img.gz", "size": 9000}]
    if with_delta:
        reps.append({"format": "ocdl", "url": "https://x/n6.delta.gz", "size": 1200,
                     "base_payload_version": 16777216})
    body = {"schema": 1, "board_id": 7, "product": "OPENMV_N6", "version": "2.1.0",
            "payload_version": 33685760, "min_platform_version": 0, "size": 16384,
            "sha256": "ab" * 32, "representations": reps}
    m = Manifest(body=body, key_id=key_id, sig_alg=ES256)
    m.signature = sign_region(priv, signed_region(m), spec)
    (d / "m.bin").write_bytes(pack_manifest(m))
    # the trusted set carries the signer's *real* pubkey under `key_id`
    write_trusted_keys(d / "trusted_keys.json",
                       [TrustedKey(key_id, ES256, "ota", public_point_hex(priv.public_key()))])
    base = bytes(range(256)) * 64
    target = base[:1000] + b"NEW" * 30 + base[1000:]
    (d / "base.img").write_bytes(base)
    (d / "target.img").write_bytes(target)
    (d / "n6.delta.gz").write_bytes(gzip.compress(make_delta(base, target)))


def test_inspect_manifest(tmp_path, capsys):
    _write_manifest_and_keys(tmp_path)
    assert main(["build", "inspect", str(tmp_path / "m.bin")]) == 0
    out = capsys.readouterr().out
    assert "manifest" in out and "board_id 7" in out and "ocdl" in out and "full" in out


def test_inspect_manifest_json(tmp_path, capsys):
    _write_manifest_and_keys(tmp_path)
    assert main(["build", "inspect", str(tmp_path / "m.bin"), "--json"]) == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert out["body"]["board_id"] == 7 and out["key_id"] == 0x0100


def test_inspect_manifest_corrupt(tmp_path, capsys):
    (tmp_path / "bad.bin").write_bytes(b"OMVM" + b"\x00" * 40)   # OMVM magic, junk body
    assert main(["build", "inspect", str(tmp_path / "bad.bin")]) == 2
    assert "not a valid manifest" in capsys.readouterr().err


def test_inspect_delta(tmp_path, capsys):
    _write_manifest_and_keys(tmp_path)
    assert main(["build", "inspect", str(tmp_path / "n6.delta.gz")]) == 0
    out = capsys.readouterr().out
    assert "delta" in out and "reconstructs" in out and "copy-with-diff" in out


def test_inspect_delta_json(tmp_path, capsys):
    _write_manifest_and_keys(tmp_path)
    assert main(["build", "inspect", str(tmp_path / "n6.delta.gz"), "--json"]) == 0
    import json
    assert json.loads(capsys.readouterr().out)["ops"] >= 1


def test_inspect_delta_corrupt(tmp_path, capsys):
    (tmp_path / "bad.delta").write_bytes(b"OCDL\xff")            # OCDL magic, truncated
    assert main(["build", "inspect", str(tmp_path / "bad.delta")]) == 2


def test_verify_manifest_ok(tmp_path, capsys):
    _write_manifest_and_keys(tmp_path)
    rc = main(["build", "verify", str(tmp_path / "m.bin"),
               "--trusted-keys", str(tmp_path / "trusted_keys.json")])
    assert rc == 0 and "verified" in capsys.readouterr().out


def test_verify_manifest_untrusted(tmp_path, capsys):
    _write_manifest_and_keys(tmp_path, key_id=0x0100)
    # a trusted set that doesn't contain the signer -> verification fails
    from openmv_ota.ota.keys import TrustedKey, write_trusted_keys
    from openmv_ota.ota import ES256
    write_trusted_keys(tmp_path / "other.json",
                       [TrustedKey(0x0999, ES256, "ota", "04" + "00" * 64)])
    rc = main(["build", "verify", str(tmp_path / "m.bin"),
               "--trusted-keys", str(tmp_path / "other.json")])
    assert rc == 1 and "FAILED" in capsys.readouterr().err


def test_verify_delta_with_base(tmp_path, capsys):
    _write_manifest_and_keys(tmp_path)
    rc = main(["build", "verify", str(tmp_path / "n6.delta.gz"),
               "--base", str(tmp_path / "base.img")])
    assert rc == 0 and "applies against the base" in capsys.readouterr().out


def test_verify_delta_with_target(tmp_path, capsys):
    _write_manifest_and_keys(tmp_path)
    rc = main(["build", "verify", str(tmp_path / "n6.delta.gz"),
               "--base", str(tmp_path / "base.img"), "--target", str(tmp_path / "target.img")])
    assert rc == 0 and "reconstructs the target" in capsys.readouterr().out


def test_verify_delta_wrong_target(tmp_path, capsys):
    _write_manifest_and_keys(tmp_path)
    (tmp_path / "wrong.img").write_bytes(b"not the target")
    rc = main(["build", "verify", str(tmp_path / "n6.delta.gz"),
               "--base", str(tmp_path / "base.img"), "--target", str(tmp_path / "wrong.img")])
    assert rc == 1 and "does not reconstruct" in capsys.readouterr().err


def test_verify_delta_needs_base(tmp_path, capsys):
    _write_manifest_and_keys(tmp_path)
    rc = main(["build", "verify", str(tmp_path / "n6.delta.gz")])
    assert rc == 2 and "needs --base" in capsys.readouterr().err


def test_verify_delta_bad_base(tmp_path, capsys):
    _write_manifest_and_keys(tmp_path)
    (tmp_path / "tiny.img").write_bytes(b"x")               # too small -> apply out of bounds
    rc = main(["build", "verify", str(tmp_path / "n6.delta.gz"),
               "--base", str(tmp_path / "tiny.img")])
    assert rc == 2 and "error" in capsys.readouterr().err


def test_load_artifact_gz_not_delta(tmp_path):
    import gzip
    from openmv_ota.build.cli import _load_artifact
    (tmp_path / "x.gz").write_bytes(gzip.compress(b"just some gzipped data, not a patch"))
    assert _load_artifact(tmp_path / "x.gz") == (None, None)
    assert _load_artifact(tmp_path / "nope.bin") == (None, None)   # unreadable
    (tmp_path / "notgz.gz").write_bytes(b"\x1f\x8bnotgzip")        # gzip magic, bad stream
    assert _load_artifact(tmp_path / "notgz.gz") == (None, None)
