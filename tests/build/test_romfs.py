"""Tests for the build_romfs orchestration."""

from __future__ import annotations

import shutil

from openmv_ota.build import romfs as build_mod
from openmv_ota.build.errors import BuildError
from openmv_ota.romfs.builder import read_image

import pytest


def _names(image_path):
    return {p for p, e in read_image(image_path.read_bytes()).walk()}


def _read_file(body: bytes, name: str) -> bytes:
    return next(e.data for p, e in read_image(body).walk() if p == name)


def _fake_compile(mpy_cross, args, src, out):
    out.write_bytes(b"MPY:" + src.read_bytes())


def test_pack_only(make_project):
    root, repo, app = make_project()
    results = build_mod.build_romfs(root, app=app, firmware=repo,
                                    compile_py=False, convert_models=False)
    assert len(results) == 1
    names = _names(results[0].output)
    assert "main.py" in names and "lib/util.py" in names and "net.tflite" in names


def test_system_json_packed_for_non_ota(make_project):
    import json
    root, repo, app = make_project(
        app_files={"main.py": "print(1)\n",
                   "settings.json": '{"app_version": "2.0.0", "vendor": "Acme"}\n'},
        extra_config='\n[targets.OPENMV_N6]\nboard_id = 7\nboard_name = "Widget"\n',
    )
    out = build_mod.build_romfs(root, app=app, firmware=repo,
                                compile_py=False, convert_models=False)[0].output
    info = json.loads(_read_file(out.read_bytes(), "system.json"))
    assert info["ota"] is False
    assert info["board"] == "OPENMV_N6" and info["board_id"] == 7
    assert info["board_name"] == "Widget"
    assert info["app_version"] == "2.0.0" and info["vendor"] == "Acme"
    assert info["firmware"]["version"] == "5.0.0"


def test_system_json_board_id_derived_for_non_ota(make_project):
    # With no board_id pinned in config, a non-OTA image still gets a stable,
    # auto-derived board_id in system.json.
    import json

    from openmv_ota.project.config import derive_board_id
    root, repo, app = make_project()
    info = json.loads(_read_file(
        build_mod.build_romfs(root, app=app, firmware=repo,
                              compile_py=False, convert_models=False)[0].output.read_bytes(),
        "system.json"))
    assert info["board_id"] == derive_board_id(info["product"], "OPENMV_N6") != 0


def test_board_name_defaults_to_product(make_project):
    # With no board_name set, system.json's board_name is the product name.
    import json
    root, repo, app = make_project(product="Gadget")
    info = json.loads(_read_file(
        build_mod.build_romfs(root, app=app, firmware=repo,
                              compile_py=False, convert_models=False)[0].output.read_bytes(),
        "system.json"))
    assert info["product"] == "Gadget" and info["board_name"] == "Gadget"


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
    from openmv_ota.ota import geometry
    from openmv_ota.project import load_project

    root, repo, app = make_project(ota=True)
    target = load_project(root, firmware=repo).board("OPENMV_N6")
    results = build_mod.build_romfs(root, app=app, firmware=repo,
                                    compile_py=False, convert_models=False)
    r = results[0]
    assert r.bound == "OTA slot"
    assert r.capacity == target.front_size - geometry.slot_overhead(target.erase_size)
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


def test_multi_partition_builds_both_role_named(make_project):
    # A multi-core board builds every partition automatically: the main partition keeps
    # the bare board name; the coprocessor (slaved) gets a -coprocessor suffix and is
    # always a plain romfs. (app-coprocessor/ is scaffolded by project new.)
    root, repo, app = make_project(boards=("OPENMV_AE3",))
    results = build_mod.build_romfs(root, app=app, firmware=repo,
                                    compile_py=False, convert_models=False)
    outs = {r.output.name for r in results}
    assert outs == {"OPENMV_AE3-romfs.img", "OPENMV_AE3-coprocessor-romfs.img"}
    copro = next(r for r in results if r.partition_index == 1)
    assert copro.output.name == "OPENMV_AE3-coprocessor-romfs.img" and not copro.ota


def test_ota_build_romfs_ae3_main_zip_coprocessor_plain(make_project):
    # In an OTA project the main partition is a signed .zip bundle, but the coprocessor
    # is still a plain .img -- the helper core can't verify, so it's never OTA.
    root, repo, app = make_project(boards=("OPENMV_AE3",), ota=True)
    results = build_mod.build_romfs(root, app=app, firmware=repo,
                                    compile_py=False, convert_models=False)
    by_idx = {r.partition_index: r for r in results}
    assert by_idx[0].output.name == "OPENMV_AE3-romfs.zip" and by_idx[0].ota
    assert by_idx[1].output.name == "OPENMV_AE3-coprocessor-romfs.img" and not by_idx[1].ota


def test_build_nests_coprocessor_into_main(make_project):
    # An OTA AE3 build nests the real coprocessor romfs into the main's runtime lib
    # (so sync() can write it on-device), with a board-specific resources.json. Build
    # from the scaffolded project app/ (no --app override) so lib/openmv_ota/ is staged.
    import json

    from openmv_ota.ota import bundle
    from openmv_ota.romfs.builder import read_image
    root, repo, _ = make_project(boards=("OPENMV_AE3",), ota=True)
    results = build_mod.build_romfs(root, firmware=repo, compile_py=False, convert_models=False)
    main = next(r for r in results if r.partition_index == 0)
    copro = next(r for r in results if r.partition_index == 1)
    body, _trailer = bundle.read_bundle(main.output)
    files = {p: e for p, e in read_image(body).walk()}
    # nested image is the *real* coprocessor romfs, not the empty placeholder
    assert files["lib/openmv_ota/data/coprocessor.romfs"].data == copro.output.read_bytes()
    manifest = json.loads(files["lib/openmv_ota/data/resources.json"].data)
    assert manifest[0]["partition"] == 1 and manifest[0]["name"] == "High Efficiency Core"


def test_build_keeps_installer_strips_coprocessor_data_for_plain_board(make_project):
    # AE3 + N6 OTA project: the shared app/ has lib/openmv_ota/data/ (scaffolded for
    # AE3). Building N6 keeps the installer + CA bundle (every OTA image needs them) but
    # strips the coprocessor resource so sync() no-ops on the board with no coprocessor.
    from openmv_ota.ota import bundle
    from openmv_ota.romfs.builder import read_image
    root, repo, _ = make_project(boards=("OPENMV_N6", "OPENMV_AE3"), ota=True)
    results = build_mod.build_romfs(root, firmware=repo, boards=["OPENMV_N6"],
                                    compile_py=False, convert_models=False)
    body, _ = bundle.read_bundle(results[0].output)
    paths = {p for p, _ in read_image(body).walk()}
    assert "lib/openmv_ota/__init__.py" in paths
    assert "lib/openmv_ota/data/installer.py" in paths
    assert "lib/openmv_ota/data/ca.pem" in paths
    assert "lib/openmv_ota/data/coprocessor.romfs" not in paths
    assert "lib/openmv_ota/data/resources.json" not in paths


def test_installer_source_not_compiled(monkeypatch, make_project):
    # install() exec()s the installer into RAM, so it must survive as source: the build
    # compiles every other .py to .mpy but leaves lib/openmv_ota/data/installer.py alone.
    from openmv_ota.ota import bundle
    monkeypatch.setattr(build_mod.mpy, "compile_py", _fake_compile)
    root, repo, _ = make_project(boards=("OPENMV_N6",), ota=True)
    results = build_mod.build_romfs(root, firmware=repo, convert_models=False)
    body, _ = bundle.read_bundle(results[0].output)
    names = {p for p, _ in read_image(body).walk()}
    assert "lib/openmv_ota/data/installer.py" in names       # kept as source
    assert "lib/openmv_ota/data/installer.mpy" not in names
    assert "lib/openmv_ota/__init__.mpy" in names            # the rest still compiled
    assert "lib/openmv_ota/__init__.py" not in names


def test_coprocessor_missing_app_dir_errors(make_project):
    # If a multi-core board's app-coprocessor/ is gone, the build fails cleanly rather
    # than silently skipping the helper partition.
    root, repo, app = make_project(boards=("OPENMV_AE3",))
    shutil.rmtree(root / "app-coprocessor")
    with pytest.raises(BuildError, match="app directory not found"):
        build_mod.build_romfs(root, app=app, firmware=repo,
                              compile_py=False, convert_models=False)


def test_keep_build_dir(make_project):
    root, repo, app = make_project()
    results = build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False,
                                    convert_models=False, keep_build_dir=True)
    assert results[0].build_dir is not None and results[0].build_dir.exists()


def test_default_app_and_output_dirs(monkeypatch, make_project):
    # No --app/--output: defaults to <project>/app and <project>/build.
    root, repo, app = make_project()
    import shutil
    # `new` scaffolds a starter app/ for every project, so merge over it.
    shutil.copytree(app, root / "app", dirs_exist_ok=True)
    results = build_mod.build_romfs(root, firmware=repo, compile_py=False, convert_models=False)
    assert results[0].output == root / "build" / "OPENMV_N6-romfs.img"


def test_drift_refuses(make_project, git_cmd):
    root, repo, app = make_project()
    (repo / "newfile.txt").write_text("x")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "drift")
    with pytest.raises(BuildError, match="refusing to proceed"):
        build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)


# --- OTA trailer signing ----------------------------------------------------

def _build_ota(make_project, **over):
    settings = over.pop("settings", '{"app_version": "1.2.3", "vendor": "Acme"}\n')
    files = {"main.py": "print(1)\n", "settings.json": settings}
    root, repo, app = make_project(ota=True, app_files=files, **over)
    return root, repo, app


def _set_board_id(root, value):
    import re
    from openmv_ota.project import ProjectPaths
    cfg = ProjectPaths(root).config
    cfg.write_text(re.sub(r"board_id   = \d+", "board_id   = %d" % value, cfg.read_text(), count=1))


def _read_bundle(result):
    """(body, trailer_bytes) from an OTA build's <board>-romfs.zip."""
    from openmv_ota.ota import bundle
    return bundle.read_bundle(result.output)


def test_ota_build_emits_bundle(make_project):
    # OTA build writes one <board>-romfs.zip with romfs.img + trailer.bin.
    import zipfile
    root, repo, app = _build_ota(make_project)
    r = build_mod.build_romfs(root, app=app, firmware=repo,
                              compile_py=False, convert_models=False)[0]
    assert r.output.name == "OPENMV_N6-romfs.zip" and r.ota
    assert set(zipfile.ZipFile(r.output).namelist()) == {"romfs.img", "trailer.bin"}


def test_ota_build_signs_and_verifies(make_project):
    import hashlib

    from openmv_ota.ota import (
        algorithm_for, parse_trailer, public_key_from_hex, read_trusted_keys,
        signed_region, verify_region,
    )
    from openmv_ota.ota.version import encode_app_version
    from openmv_ota.project import ProjectPaths

    root, repo, app = _build_ota(make_project)
    _set_board_id(root, 999)  # config-only identity, no drift
    r = build_mod.build_romfs(root, app=app, firmware=repo,
                              compile_py=False, convert_models=False)[0]

    body, trailer_bytes = _read_bundle(r)
    t = parse_trailer(trailer_bytes)
    assert t.payload_version == encode_app_version("1.2.3")
    assert t.board_id == 999
    assert t.body_sha256 == hashlib.sha256(body).digest()
    assert t.meta["vendor"] == "Acme" and t.meta["app_version"] == "1.2.3"
    assert t.meta["firmware"]["version"] == "5.0.0"

    # The signature verifies against the project's committed trusted public key.
    entry = next(k for k in read_trusted_keys(ProjectPaths(root).trusted_keys)
                 if k.key_id == t.key_id)
    alg = algorithm_for(entry.alg)
    pub = public_key_from_hex(entry.pubkey, alg)
    assert verify_region(pub, signed_region(trailer_bytes), t.signature, alg) is True


def test_ota_trailer_pad_size_is_correct_and_signed(make_project):
    # pad_size is computed from the slot geometry and lands in the signed region:
    # body_size + pad_size == the status-sector offset (front_size - 2 blocks).
    from openmv_ota.ota import geometry, parse_trailer
    from openmv_ota.project import load_project

    root, repo, app = _build_ota(make_project)
    target = load_project(root, firmware=repo).board("OPENMV_N6")
    r = build_mod.build_romfs(root, app=app, firmware=repo,
                              compile_py=False, convert_models=False)[0]
    t = parse_trailer(_read_bundle(r)[1])
    overhead = geometry.slot_overhead(target.erase_size)
    assert t.body_size == r.size
    assert t.body_size + t.pad_size == target.front_size - overhead


def test_ota_trailer_meta_mirrors_system_json(make_project):
    import json

    from openmv_ota.ota import parse_trailer

    root, repo, app = _build_ota(make_project)
    _set_board_id(root, 42)
    r = build_mod.build_romfs(root, app=app, firmware=repo,
                              compile_py=False, convert_models=False)[0]
    body, trailer_bytes = _read_bundle(r)
    info = json.loads(_read_file(body, "system.json"))
    # The trailer carries a verbatim copy of the ROMFS system.json.
    assert parse_trailer(trailer_bytes).meta == info
    assert info["ota"] is True and info["board_id"] == 42 and info["app_version"] == "1.2.3"


def test_ota_build_sets_rollback_floor(make_project):
    from openmv_ota.ota import parse_trailer
    from openmv_ota.ota.version import encode_app_version

    root, repo, app = _build_ota(
        make_project, settings='{"app_version": "2.5.0", "rollback_floor": "2.0.0"}\n')
    r = build_mod.build_romfs(root, app=app, firmware=repo,
                              compile_py=False, convert_models=False)[0]
    t = parse_trailer(_read_bundle(r)[1])
    assert t.payload_version_floor == encode_app_version("2.0.0")
    assert t.payload_version == encode_app_version("2.5.0")


def test_ota_build_floor_above_version_errors(make_project):
    root, repo, app = _build_ota(
        make_project, settings='{"app_version": "2.0.0", "rollback_floor": "3.0.0"}\n')
    with pytest.raises(BuildError, match="rollback_floor 3.0.0 can't exceed app_version"):
        build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)


def test_ota_build_bad_floor_semver(make_project):
    root, repo, app = _build_ota(
        make_project, settings='{"app_version": "2.0.0", "rollback_floor": "2.0"}\n')
    with pytest.raises(BuildError, match="invalid rollback_floor"):
        build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)


def test_ota_build_warns_on_unset_board_id(make_project, capsys):
    root, repo, app = _build_ota(make_project)
    _set_board_id(root, 0)  # explicitly clear the auto-assigned id
    build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)
    assert "board_id 0" in capsys.readouterr().err


def test_build_warns_on_board_id_collision(capsys):
    from openmv_ota.build.romfs import _warn_board_id_collisions
    from openmv_ota.project.config import OtaConfig

    cfg = OtaConfig(
        name="p", vendor=None, boards=["A", "B", "C"],
        overrides={"A": {"board_id": 5}, "B": {"board_id": 5}, "C": {"board_id": 0}},
    )
    _warn_board_id_collisions(cfg)
    err = capsys.readouterr().err
    assert "board_id 5 is shared by A and B" in err


def test_ota_build_missing_settings(make_project):
    root, repo, app = _build_ota(make_project)
    (app / "settings.json").unlink()
    with pytest.raises(BuildError, match="needs a readable"):
        build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)


def test_ota_build_missing_app_version(make_project):
    root, repo, app = _build_ota(make_project, settings='{"vendor": "x"}\n')
    with pytest.raises(BuildError, match="missing app_version"):
        build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)


def test_ota_build_bad_semver(make_project):
    root, repo, app = _build_ota(make_project, settings='{"app_version": "1.2"}\n')
    with pytest.raises(BuildError, match="invalid app version"):
        build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)


def test_ota_build_missing_private_key(make_project):
    from openmv_ota.project import ProjectPaths
    root, repo, app = _build_ota(make_project)
    (ProjectPaths(root).private_keys_dir / "ota-0100.pem").unlink()
    with pytest.raises(BuildError, match="private key .* not found"):
        build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)


def test_ota_build_refuses_revoked_signing_key(make_project):
    from openmv_ota.project import keys as keys_mod
    root, repo, app = _build_ota(make_project)
    keys_mod.revoke_key(root, 0x0100)  # revoke the current signing key
    with pytest.raises(BuildError, match="is revoked"):
        build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)


def test_ota_build_unknown_signing_key(make_project):
    from openmv_ota.project import ProjectPaths
    root, repo, app = _build_ota(make_project)
    cfg = ProjectPaths(root).config
    cfg.write_text(cfg.read_text().replace("signing_key_id = 256", "signing_key_id = 9999"))
    with pytest.raises(BuildError, match="not in keys/trusted_keys.json"):
        build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)


# --- factory image (dual-slot golden + initial FRONT) -----------------------

def test_factory_build_composes_dual_slot(make_project):
    from openmv_ota.ota import geometry, parse_trailer, status
    from openmv_ota.project import load_project

    root, repo, app = _build_ota(make_project)
    _set_board_id(root, 7)
    target = load_project(root, firmware=repo).board("OPENMV_N6")
    r = build_mod.build_factory_romfs(root, app=app, firmware=repo,
                                      compile_py=False, convert_models=False)[0]
    assert r.output.name == "OPENMV_N6-factory-romfs.img" and r.bound == "factory slot"
    img = r.output.read_bytes()
    assert len(img) == target.partition_size  # the whole partition

    block = geometry.ota_block(target.erase_size)
    front, part = target.front_size, target.partition_size
    # both slots carry the same golden body, factory-signed
    assert parse_trailer(img[front - block:]).board_id == 7        # FRONT trailer
    assert parse_trailer(img[part - block:]).board_id == 7         # BACK trailer

    fs = img[front - 2 * block: front - block]                     # FRONT status sector
    bs = img[part - 2 * block: part - block]                       # BACK status sector
    assert (fs[0:16], fs[16:32], fs[32:48]) == (status.PENDING, status.TRIED, status.CONFIRMED)
    assert (bs[0:16], bs[16:32], bs[32:48]) == (b"\xff" * 16, b"\xff" * 16, status.CONFIRMED)


def test_factory_signed_by_factory_key(make_project):
    from openmv_ota.ota import (
        algorithm_for, geometry, parse_trailer, public_key_from_hex, read_trusted_keys,
        signed_region, verify_region,
    )
    from openmv_ota.project import ProjectPaths, load_project

    root, repo, app = _build_ota(make_project)
    target = load_project(root, firmware=repo).board("OPENMV_N6")
    r = build_mod.build_factory_romfs(root, app=app, firmware=repo,
                                      compile_py=False, convert_models=False)[0]
    block = geometry.ota_block(target.erase_size)
    sector = r.output.read_bytes()[target.partition_size - block:]  # BACK trailer
    t = parse_trailer(sector)
    entry = next(k for k in read_trusted_keys(ProjectPaths(root).trusted_keys)
                 if k.key_id == t.key_id)
    assert entry.role == "factory"
    alg = algorithm_for(entry.alg)
    pub = public_key_from_hex(entry.pubkey, alg)
    assert verify_region(pub, signed_region(sector), t.signature, alg) is True


def test_factory_keep_build_dir(make_project):
    root, repo, app = _build_ota(make_project)
    r = build_mod.build_factory_romfs(root, app=app, firmware=repo, compile_py=False,
                                      convert_models=False, keep_build_dir=True)[0]
    assert r.build_dir is not None and r.build_dir.exists()


def test_factory_requires_ota_project(make_project):
    root, repo, app = make_project()  # non-OTA
    with pytest.raises(BuildError, match="needs an OTA project"):
        build_mod.build_factory_romfs(root, app=app, firmware=repo,
                                      compile_py=False, convert_models=False)


def test_factory_no_matching_targets(make_project):
    root, repo, app = _build_ota(make_project)
    with pytest.raises(BuildError, match="no matching targets"):
        build_mod.build_factory_romfs(root, app=app, firmware=repo, boards=["OPENMV_NOPE"],
                                      compile_py=False, convert_models=False)


def test_factory_coprocessor_is_plain(make_project):
    # An OTA multi-core board: the main partition gets a dual-slot factory image, but the
    # coprocessor (slaved) has no golden/trial concept -- it's the same plain romfs.
    root, repo, app = make_project(boards=("OPENMV_AE3",), ota=True)
    results = build_mod.build_factory_romfs(root, app=app, firmware=repo,
                                            compile_py=False, convert_models=False)
    by_idx = {r.partition_index: r for r in results}
    assert by_idx[0].output.name == "OPENMV_AE3-factory-romfs.img"
    assert by_idx[1].output.name == "OPENMV_AE3-coprocessor-romfs.img" and not by_idx[1].ota


def test_factory_drift_refuses(make_project, git_cmd):
    root, repo, app = _build_ota(make_project)
    (repo / "newfile.txt").write_text("x")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "drift")
    with pytest.raises(BuildError, match="refusing to proceed"):
        build_mod.build_factory_romfs(root, app=app, firmware=repo,
                                      compile_py=False, convert_models=False)


def test_factory_unknown_key(make_project):
    root, repo, app = _build_ota(make_project)
    with pytest.raises(BuildError, match="not in keys/trusted_keys.json"):
        build_mod.build_factory_romfs(root, app=app, firmware=repo, factory_key=0x9999,
                                      compile_py=False, convert_models=False)


def test_factory_rejects_ota_key(make_project):
    # 0x0100 is an OTA key, not a factory key.
    root, repo, app = _build_ota(make_project)
    with pytest.raises(BuildError, match="not a factory key"):
        build_mod.build_factory_romfs(root, app=app, firmware=repo, factory_key=0x0100,
                                      compile_py=False, convert_models=False)


def test_factory_body_too_big(monkeypatch, make_project):
    from openmv_ota.ota import geometry
    from openmv_ota.project import load_project

    root, repo, app = _build_ota(make_project)
    target = load_project(root, firmware=repo).board("OPENMV_N6")
    front_cap = target.front_size - geometry.slot_overhead(target.erase_size)
    monkeypatch.setattr(build_mod, "build_image", lambda *a, **k: b"\x00" * (front_cap + 1))
    with pytest.raises(BuildError, match="factory slot holds"):
        build_mod.build_factory_romfs(root, app=app, firmware=repo,
                                      compile_py=False, convert_models=False)


# --- build_ota_image (the gzipped FRONT-slot download image) -----------------

def _build_n6_ota_bundle(make_project):
    root, repo, _ = make_project(boards=("OPENMV_N6",), ota=True)
    build_mod.build_romfs(root, firmware=repo, compile_py=False, convert_models=False)
    return root, repo


def test_build_ota_image_roundtrip(make_project):
    import gzip

    from openmv_ota.ota import bundle, geometry, parse_trailer
    from openmv_ota.project import load_project
    root, repo = _build_n6_ota_bundle(make_project)

    results = build_mod.build_ota_image(root, firmware=repo)
    assert len(results) == 1
    r = results[0]
    assert r.output.name == "OPENMV_N6-ota.img.gz"
    assert r.gz_size < r.image_size                       # the 0xFF gap compresses away

    image = gzip.decompress(r.output.read_bytes())
    t = load_project(root, firmware=repo).board("OPENMV_N6")
    front_size, block = t.front_size, geometry.ota_block(t.erase_size)
    body, trailer = bundle.read_bundle(root / "build" / "OPENMV_N6-romfs.zip")

    assert len(image) == front_size                       # a full slot, 1:1 writable
    assert image[:len(body)] == body                      # body at offset 0
    assert image[front_size - 2 * block:front_size - block] == b"\xff" * block  # blank status
    assert image[front_size - block:front_size - block + len(trailer)] == trailer
    parse_trailer(image[front_size - block:])             # the trailer parses in place


def test_build_ota_image_non_ota_errors(make_project):
    root, repo, _ = make_project(boards=("OPENMV_N6",))   # not an OTA project
    with pytest.raises(BuildError, match="needs an OTA project"):
        build_mod.build_ota_image(root, firmware=repo)


def test_build_ota_image_missing_bundle_errors(make_project):
    root, repo, _ = make_project(boards=("OPENMV_N6",), ota=True)  # no `build romfs` yet
    with pytest.raises(BuildError, match="not found"):
        build_mod.build_ota_image(root, firmware=repo)


def test_build_ota_image_bad_bundle_errors(make_project):
    root, repo, _ = make_project(boards=("OPENMV_N6",), ota=True)
    (root / "build").mkdir(exist_ok=True)
    (root / "build" / "OPENMV_N6-romfs.zip").write_bytes(b"not a zip")
    with pytest.raises(BuildError):
        build_mod.build_ota_image(root, firmware=repo)


def test_build_ota_image_oversize_body_errors(make_project):
    from openmv_ota.ota import bundle, geometry
    from openmv_ota.project import load_project
    root, repo = _build_n6_ota_bundle(make_project)
    t = load_project(root, firmware=repo).board("OPENMV_N6")
    front_cap = t.front_size - 2 * geometry.ota_block(t.erase_size)
    bundle.write_bundle(root / "build" / "OPENMV_N6-romfs.zip",
                        b"\x00" * (front_cap + 1), b"trailer")
    with pytest.raises(BuildError, match="body is"):
        build_mod.build_ota_image(root, firmware=repo)


def test_build_ota_image_no_targets_errors(make_project):
    root, repo = _build_n6_ota_bundle(make_project)
    with pytest.raises(BuildError, match="no matching"):
        build_mod.build_ota_image(root, firmware=repo, boards=["NOPE"])


def test_build_ota_image_bad_project_errors(make_project, tmp_path):
    root, _repo, _ = make_project(boards=("OPENMV_N6",), ota=True)
    with pytest.raises(BuildError):                       # firmware override isn't a git repo
        build_mod.build_ota_image(root, firmware=tmp_path / "not-a-repo")


# --- build_manifest (the signed update descriptor) ---------------------------

_URL = "https://dl.example.io/fw"


def _build_n6_ota_artifacts(make_project):
    root, repo = _build_n6_ota_bundle(make_project)
    build_mod.build_ota_image(root, firmware=repo)        # produces the .img.gz
    return root, repo


def test_build_manifest_roundtrip(make_project):
    import gzip
    import hashlib

    from openmv_ota.ota import parse_trailer
    from openmv_ota.ota.keys import read_trusted_keys
    from openmv_ota.ota.manifest import parse_manifest, select_representation
    from openmv_ota.ota.verify import verify_manifest
    from openmv_ota.project.project import ProjectPaths
    root, repo = _build_n6_ota_artifacts(make_project)

    results = build_mod.build_manifest(root, url_base=_URL, firmware=repo)
    assert len(results) == 1 and results[0].output.name == "OPENMV_N6-manifest.bin"

    raw = results[0].output.read_bytes()
    trusted = read_trusted_keys(ProjectPaths(root).trusted_keys)
    ok, _reason = verify_manifest(raw, trusted)            # signed by the project OTA key
    assert ok

    img = root / "build" / "OPENMV_N6-ota.img.gz"
    image = gzip.decompress(img.read_bytes())
    body = parse_manifest(raw).body
    assert body["sha256"] == hashlib.sha256(image).hexdigest()
    assert body["size"] == len(image)

    # board_id / payload_version are taken from the image's own signed trailer
    from openmv_ota.ota import bundle as _bundle
    _b, trailer = _bundle.read_bundle(root / "build" / "OPENMV_N6-romfs.zip")
    expect = parse_trailer(trailer)
    assert body["board_id"] == expect.board_id
    assert body["payload_version"] == expect.payload_version

    rep = select_representation(body, delta_capable=False, golden_payload_version=0)
    assert rep["format"] == "full"
    assert rep["url"] == _URL + "/OPENMV_N6-ota.img.gz"
    assert rep["size"] == img.stat().st_size


def test_build_manifest_requires_https_url(make_project):
    root, repo = _build_n6_ota_artifacts(make_project)
    with pytest.raises(BuildError, match="https://"):
        build_mod.build_manifest(root, url_base="http://insecure/fw", firmware=repo)


def test_build_manifest_non_ota_errors(make_project):
    root, repo, _ = make_project(boards=("OPENMV_N6",))   # not an OTA project
    with pytest.raises(BuildError, match="needs an OTA project"):
        build_mod.build_manifest(root, url_base=_URL, firmware=repo)


def test_build_manifest_missing_image_errors(make_project):
    root, repo = _build_n6_ota_bundle(make_project)        # bundle built, but no ota-image
    with pytest.raises(BuildError, match="run `openmv-ota build ota-image`"):
        build_mod.build_manifest(root, url_base=_URL, firmware=repo)


def test_build_manifest_no_targets_errors(make_project):
    root, repo = _build_n6_ota_artifacts(make_project)
    with pytest.raises(BuildError, match="no matching"):
        build_mod.build_manifest(root, url_base=_URL, firmware=repo, boards=["NOPE"])


def test_build_manifest_bad_bundle_errors(make_project):
    # the image exists but the bundle it reads board_id/version from is corrupt
    root, repo = _build_n6_ota_artifacts(make_project)
    (root / "build" / "OPENMV_N6-romfs.zip").write_bytes(b"not a zip")
    with pytest.raises(BuildError):
        build_mod.build_manifest(root, url_base=_URL, firmware=repo)


# --- build_delta + manifest delta representation -----------------------------

def test_build_delta_roundtrip(make_project, tmp_path):
    import gzip

    from openmv_ota.ota.delta import apply_delta
    base = tmp_path / "golden.img"
    target = tmp_path / "new.img"
    base_bytes = bytes(range(256)) * 200
    target_bytes = base_bytes[:5000] + b"NEW" * 100 + base_bytes[5000:]
    base.write_bytes(base_bytes)
    target.write_bytes(gzip.compress(target_bytes))       # .img but gzip-magic -> gunzipped
    out = tmp_path / "v1-to-v2.delta.gz"
    r = build_mod.build_delta(base, target, out)
    assert r.output == out and r.target_size == len(target_bytes)
    assert r.gz_size < r.target_size                       # the delta is much smaller
    assert apply_delta(base_bytes, gzip.decompress(out.read_bytes())) == target_bytes


def test_build_delta_self_check_guards(make_project, tmp_path, monkeypatch):
    base = tmp_path / "b.img"
    target = tmp_path / "t.img"
    base.write_bytes(b"A" * 4096)
    target.write_bytes(b"B" * 4096)
    from openmv_ota.ota import delta as delta_mod
    # (a) make_delta emits a structurally invalid patch -> apply_delta raises -> self-check
    monkeypatch.setattr(delta_mod, "make_delta", lambda b, t: b"NOTOCDL!")
    with pytest.raises(BuildError, match="self-check failed"):
        build_mod.build_delta(base, target, tmp_path / "out.gz")
    # (b) a valid-but-empty patch applies cleanly but doesn't reproduce target -> self-check
    monkeypatch.setattr(delta_mod, "make_delta", lambda b, t: delta_mod.MAGIC + b"\x00")
    with pytest.raises(BuildError, match="does not reconstruct"):
        build_mod.build_delta(base, target, tmp_path / "out2.gz")


def test_build_manifest_with_delta_rep(make_project):
    import gzip

    from openmv_ota.ota.manifest import parse_manifest, select_representation
    from openmv_ota.ota.version import encode_app_version
    root, repo = _build_n6_ota_artifacts(make_project)
    img = root / "build" / "OPENMV_N6-ota.img.gz"
    image = gzip.decompress(img.read_bytes())

    # a golden almost identical to the new image -> a tiny delta that beats the full
    base_bytes = bytearray(image)
    base_bytes[100:110] = b"OLDOLDOLD!"
    delta_path = root / "build" / "OPENMV_N6-v0-to-v1.delta.gz"
    base_file = root / "build" / "golden.img"
    base_file.write_bytes(bytes(base_bytes))
    new_file = root / "build" / "new.img"
    new_file.write_bytes(image)
    dr = build_mod.build_delta(base_file, new_file, delta_path)
    assert dr.gz_size < img.stat().st_size                 # delta beats the full download

    results = build_mod.build_manifest(root, url_base=_URL, firmware=repo, boards=["OPENMV_N6"],
                                       delta=delta_path, delta_base_version="1.0.0")
    body = parse_manifest(results[0].output.read_bytes()).body
    fmts = {r["format"] for r in body["representations"]}
    assert fmts == {"full", "ocdl"}
    rep = select_representation(body, delta_capable=True,
                               golden_payload_version=encode_app_version("1.0.0"))
    assert rep["format"] == "ocdl" and rep["url"] == _URL + "/" + delta_path.name


def test_build_manifest_delta_needs_base_version(make_project):
    root, repo = _build_n6_ota_artifacts(make_project)
    (root / "build" / "x.delta.gz").write_bytes(b"whatever")
    with pytest.raises(BuildError, match="delta-base-version"):
        build_mod.build_manifest(root, url_base=_URL, firmware=repo,
                                 delta=root / "build" / "x.delta.gz")


def test_build_manifest_delta_size_mismatch(make_project):
    import gzip

    from openmv_ota.ota.delta import make_delta
    root, repo = _build_n6_ota_artifacts(make_project)
    # a delta that reconstructs the WRONG size -> manifest must refuse it
    bad = gzip.compress(make_delta(b"x" * 10, b"y" * 20))
    bad_path = root / "build" / "bad.delta.gz"
    bad_path.write_bytes(bad)
    with pytest.raises(BuildError, match="reconstructs"):
        build_mod.build_manifest(root, url_base=_URL, firmware=repo, boards=["OPENMV_N6"],
                                 delta=bad_path, delta_base_version="1.0.0")


def test_build_manifest_delta_bad_magic(make_project):
    import gzip
    root, repo = _build_n6_ota_artifacts(make_project)
    bad_path = root / "build" / "junk.delta.gz"
    bad_path.write_bytes(gzip.compress(b"NOT-AN-OCDL-PATCH"))   # gunzips to bad magic
    with pytest.raises(BuildError, match="bad delta"):
        build_mod.build_manifest(root, url_base=_URL, firmware=repo, boards=["OPENMV_N6"],
                                 delta=bad_path, delta_base_version="1.0.0")


def test_build_manifest_delta_rejects_multiple_boards(make_project):
    # --delta names one artifact, so it's only valid when one board is selected
    root, repo, _ = make_project(boards=("OPENMV_N6", "OPENMV_AE3"), ota=True)
    (root / "build").mkdir(exist_ok=True)
    (root / "build" / "d.delta.gz").write_bytes(b"x")
    with pytest.raises(BuildError, match="one board"):
        build_mod.build_manifest(root, url_base=_URL, firmware=repo,
                                 delta=root / "build" / "d.delta.gz", delta_base_version="1.0.0")


def test_build_manifest_bad_project_errors(make_project, tmp_path):
    root, _repo, _ = make_project(boards=("OPENMV_N6",), ota=True)
    with pytest.raises(BuildError):
        build_mod.build_manifest(root, url_base=_URL, firmware=tmp_path / "not-a-repo")


# --- build_ota_romfs (the one cloud-publish verb) ----------------------------

def _ota_project_with_factory(make_project):
    root, repo, _ = make_project(boards=("OPENMV_N6",), ota=True)
    build_mod.build_romfs(root, firmware=repo, compile_py=False, convert_models=False)
    build_mod.build_factory_romfs(root, firmware=repo, compile_py=False, convert_models=False)
    return root, repo


def test_build_ota_romfs_relative_default(make_project):
    from openmv_ota.ota.manifest import parse_manifest
    root, repo = _build_n6_ota_bundle(make_project)
    [r] = build_mod.build_ota_romfs(root, firmware=repo)
    assert r.image.name == "OPENMV_N6-ota.img.gz" and r.delta is None
    body = parse_manifest(r.manifest.read_bytes()).body
    assert body["representations"][0]["url"] == "OPENMV_N6-ota.img.gz"   # relative filename


def test_build_ota_romfs_absolute_url_base(make_project):
    from openmv_ota.ota.manifest import parse_manifest
    root, repo = _build_n6_ota_bundle(make_project)
    [r] = build_mod.build_ota_romfs(root, url_base="https://dl.x.io/fw/", firmware=repo)
    body = parse_manifest(r.manifest.read_bytes()).body
    assert body["representations"][0]["url"] == "https://dl.x.io/fw/OPENMV_N6-ota.img.gz"


def test_build_ota_romfs_with_delta_from_file(make_project):
    from openmv_ota.ota.delta import apply_delta
    from openmv_ota.ota.manifest import parse_manifest, select_representation
    from openmv_ota.project import load_project
    root, repo = _ota_project_with_factory(make_project)
    factory = root / "build" / "OPENMV_N6-factory-romfs.img"
    [r] = build_mod.build_ota_romfs(root, firmware=repo, delta_from=factory)
    assert r.delta is not None and r.delta.name == "OPENMV_N6-ota.delta.gz"

    body = parse_manifest(r.manifest.read_bytes()).body
    assert {rep["format"] for rep in body["representations"]} == {"full", "ocdl"}
    # the delta reconstructs the new image from the factory BACK slot (what the device reads)
    import gzip
    t = load_project(root, firmware=repo).board("OPENMV_N6")
    back = factory.read_bytes()[t.front_size:]
    new_img = gzip.decompress((root / "build" / "OPENMV_N6-ota.img.gz").read_bytes())
    assert apply_delta(back, gzip.decompress(r.delta.read_bytes())) == new_img
    # the delta rep's base matches the factory golden's version, so a device on it picks delta
    ocdl = next(rep for rep in body["representations"] if rep["format"] == "ocdl")
    assert select_representation(body, delta_capable=True,
                               golden_payload_version=ocdl["base_payload_version"])["format"] == "ocdl"


def test_build_ota_romfs_delta_from_dir(make_project):
    from openmv_ota.ota.manifest import parse_manifest
    root, repo = _ota_project_with_factory(make_project)
    # a directory holding <board>-factory-romfs.img (build/ itself qualifies)
    [r] = build_mod.build_ota_romfs(root, firmware=repo, delta_from=root / "build")
    body = parse_manifest(r.manifest.read_bytes()).body
    assert {rep["format"] for rep in body["representations"]} == {"full", "ocdl"}


def test_build_ota_romfs_delta_dir_missing_golden_warns(make_project, tmp_path, capsys):
    from openmv_ota.ota.manifest import parse_manifest
    root, repo = _build_n6_ota_bundle(make_project)
    empty = tmp_path / "no-goldens"
    empty.mkdir()
    [r] = build_mod.build_ota_romfs(root, firmware=repo, delta_from=empty)
    assert r.delta is None                                  # no golden -> full image only
    assert "no factory golden" in capsys.readouterr().err
    body = parse_manifest(r.manifest.read_bytes()).body
    assert {rep["format"] for rep in body["representations"]} == {"full"}


def test_build_ota_romfs_delta_file_rejects_multiple_boards(make_project):
    root, repo, _ = make_project(boards=("OPENMV_N6", "OPENMV_AE3"), ota=True)
    build_mod.build_romfs(root, firmware=repo, compile_py=False, convert_models=False)
    (root / "build" / "fac.img").write_bytes(b"x")
    with pytest.raises(BuildError, match="single file needs one board"):
        build_mod.build_ota_romfs(root, firmware=repo, delta_from=root / "build" / "fac.img")


def test_build_ota_romfs_delta_from_bad_factory(make_project):
    root, repo = _build_n6_ota_bundle(make_project)
    bad = root / "build" / "OPENMV_N6-factory-romfs.img"
    bad.write_bytes(b"not a factory image" * 100)
    with pytest.raises(BuildError, match="not a usable factory image"):
        build_mod.build_ota_romfs(root, firmware=repo, delta_from=root / "build")


def test_build_ota_romfs_no_targets(make_project):
    root, repo = _build_n6_ota_bundle(make_project)
    with pytest.raises(BuildError, match="no matching"):
        build_mod.build_ota_romfs(root, firmware=repo, boards=["NOPE"])


def test_build_ota_romfs_bad_project(make_project, tmp_path):
    root, _repo, _ = make_project(boards=("OPENMV_N6",), ota=True)
    with pytest.raises(BuildError):
        build_mod.build_ota_romfs(root, firmware=tmp_path / "not-a-repo")


def test_build_manifest_relative_default(make_project):
    from openmv_ota.ota.manifest import parse_manifest
    root, repo = _build_n6_ota_artifacts(make_project)
    [r] = build_mod.build_manifest(root, firmware=repo)     # no url_base -> relative
    body = parse_manifest(r.output.read_bytes()).body
    assert body["representations"][0]["url"] == "OPENMV_N6-ota.img.gz"
