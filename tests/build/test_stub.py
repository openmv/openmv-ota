"""``build stub``: the boot stub's keys header and build driver (openmv_ota.build.stub)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openmv_ota.build import stub as stub_mod
from openmv_ota.build.errors import BuildError
from openmv_ota.cli import main


def _fake_make(monkeypatch, *, produce=True):
    """Stand in for make: record the args, read the generated keys header while it still
    exists, and drop a stub.bin in BUILD."""
    calls: list[dict] = []

    def fake(args):
        kv = dict(a.split("=", 1) for a in args if "=" in a)
        kv["args"] = list(args)
        kv["header"] = (Path(kv["KEYS_DIR"]) / stub_mod.KEYS_HEADER).read_text()
        calls.append(kv)
        if produce:
            Path(kv["BUILD"]).mkdir(parents=True)
            (Path(kv["BUILD"]) / "stub.bin").write_bytes(b"STUB" * 100)

    monkeypatch.setattr(stub_mod, "_run_make", fake)
    return calls


def test_build_stub_generates_the_keys_header_and_copies_the_binary(make_project, monkeypatch):
    from openmv_ota.ota.keys import read_root_keys
    from openmv_ota.project.config import derive_product_id
    from openmv_ota.project.project import ProjectPaths

    calls = _fake_make(monkeypatch)
    root, repo, _app = make_project(ota=True)
    [r] = stub_mod.build_stub(root, firmware=repo, jobs=2)
    assert r.board == "OPENMV_N6" and r.output == root / "build" / "OPENMV_N6-stub.bin"
    assert r.size == 400 and r.output.read_bytes() == b"STUB" * 100

    [c] = calls
    assert c["BOARD"] == "OPENMV_N6" and c["OMV_DIR"] == str(repo.resolve()) and "-j2" in c["args"]
    assert c["args"][:2] == ["-C", str(stub_mod.STUB_DIR)]
    h = c["header"]
    roots = read_root_keys(ProjectPaths(root).root_keys)
    assert "#define STUB_PRODUCT_ID %dULL" % derive_product_id("proj", "OPENMV_N6") in h \
        or "#define STUB_PRODUCT_ID " in h
    assert "#define STUB_MAX_ATTEMPTS 3" in h
    assert "#define STUB_ROOT_KEY_COUNT %d" % len(roots) in h
    for k in roots:                                   # X || Y of each root, in root_id order
        point = bytes.fromhex(k.pubkey)[1:]
        assert "0x%02x, 0x%02x" % (point[0], point[1]) in h
    assert h.count("{ 0x") == len(roots)


def test_build_stub_names_the_sdk_toolchain_when_installed(make_project, monkeypatch):
    """The stub's Makefile has no SDK path of its own (the firmware's Makefile exports one),
    so the build passes the project SDK's arm-none-eabi- prefix when that gcc exists."""
    from openmv_ota.project.project import load_project

    calls = _fake_make(monkeypatch)
    root, repo, _app = make_project(ota=True)
    sdk = load_project(root, firmware=repo).sdk_home
    [c] = (stub_mod.build_stub(root, firmware=repo), calls)[1]
    assert not any(a.startswith("CROSS_COMPILE=") for a in c["args"])   # no gcc in the SDK dir
    gcc = sdk / "gcc" / "bin" / "arm-none-eabi-gcc"
    gcc.parent.mkdir(parents=True, exist_ok=True)
    gcc.write_text("#!/bin/sh\n")
    stub_mod.build_stub(root, firmware=repo)
    assert "CROSS_COMPILE=%s" % (str(gcc)[:-3]) in calls[-1]["args"]


def test_build_stub_honours_output_and_board_filters(make_project, monkeypatch):
    _fake_make(monkeypatch)
    root, repo, _app = make_project(ota=True)
    out = root / "elsewhere"
    [r] = stub_mod.build_stub(root, firmware=repo, output=out, boards=["OPENMV_N6"])
    assert r.output.parent == out


def test_build_stub_rejects_a_non_ota_project(make_project, monkeypatch):
    _fake_make(monkeypatch)
    root, repo, _app = make_project()
    with pytest.raises(BuildError, match="not an OTA project"):
        stub_mod.build_stub(root, firmware=repo)


def test_build_stub_rejects_unknown_boards_and_non_projects(make_project, monkeypatch, tmp_path):
    _fake_make(monkeypatch)
    root, repo, _app = make_project(ota=True)
    with pytest.raises(BuildError, match="no matching boards"):
        stub_mod.build_stub(root, firmware=repo, boards=["NOPE"])
    with pytest.raises(BuildError):                         # not a project at all
        stub_mod.build_stub(tmp_path / "nope", firmware=repo)


def test_build_stub_needs_a_stub_layout_and_root_keys(make_project, monkeypatch):
    from openmv_ota.project.project import ProjectPaths

    _fake_make(monkeypatch)
    root, repo, _app = make_project(boards=("OPENMV_N6", "OPENMV4"), ota=True)
    with pytest.raises(BuildError, match="OPENMV4 has no boot-stub layout"):
        stub_mod.build_stub(root, firmware=repo, boards=["OPENMV4"])
    ProjectPaths(root).root_keys.unlink()
    with pytest.raises(BuildError, match="root_keys"):
        stub_mod.build_stub(root, firmware=repo, boards=["OPENMV_N6"])


def test_build_stub_fails_when_make_leaves_no_binary(make_project, monkeypatch):
    _fake_make(monkeypatch, produce=False)
    root, repo, _app = make_project(ota=True)
    with pytest.raises(BuildError, match="produced no stub.bin"):
        stub_mod.build_stub(root, firmware=repo)


def test_stub_keys_header_checks_the_roots():
    from openmv_ota.ota.keys import RootKey

    good = "04" + "11" * 64
    with pytest.raises(BuildError, match="numbered 0..n-1"):
        stub_mod.stub_keys_header([RootKey(root_id=1, alg=-7, pubkey=good)], 1, 3)
    with pytest.raises(BuildError, match="numbered 0..n-1"):
        stub_mod.stub_keys_header([], 1, 3)
    with pytest.raises(BuildError, match="not an uncompressed P-256 point"):
        stub_mod.stub_keys_header([RootKey(root_id=0, alg=-7, pubkey="02" + "11" * 32)], 1, 3)
    h = stub_mod.stub_keys_header([RootKey(root_id=0, alg=-7, pubkey=good)], 5, 2)
    assert "#define STUB_PRODUCT_ID 5ULL" in h and "#define STUB_MAX_ATTEMPTS 2" in h
    assert h.count("0x11") == 64


def test_run_make_errors_become_build_errors(monkeypatch):
    def missing(*_a, **_k):
        raise FileNotFoundError("make")
    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(BuildError, match="make not found") as e:
        stub_mod._run_make(["-C", "x"])
    assert e.value.exit_code == 2

    def failing(*_a, **_k):
        raise subprocess.CalledProcessError(3, ["make"])
    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(BuildError, match="stub build failed") as e:
        stub_mod._run_make(["-C", "x"])
    assert e.value.exit_code == 3

    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, check: seen.append((cmd, check)))
    stub_mod._run_make(["-C", "x"])
    assert seen == [([stub_mod.MAKE, "-C", "x"], True)]


def test_build_stub_cli(make_project, monkeypatch, capsys):
    _fake_make(monkeypatch)
    root, repo, _app = make_project(ota=True)
    assert main(["build", "stub", str(root), "-f", str(repo), "-b", "OPENMV_N6", "-j", "1"]) == 0
    assert "Built OPENMV_N6-stub.bin  (400 bytes)" in capsys.readouterr().out
    rc = main(["build", "stub", str(root), "-f", str(repo), "-b", "NOPE"])
    assert rc != 0 and "no matching boards" in capsys.readouterr().err


def test_the_stub_sources_ship_as_package_data():
    """The C tree must be inside the package (pyproject package-data) or an installed
    tool cannot build it."""
    assert (stub_mod.STUB_DIR / "Makefile").is_file()
    assert (stub_mod.STUB_DIR / "boards" / "OPENMV_N6" / "stub_config.h").is_file()
    assert (stub_mod.STUB_DIR / "common" / "stub.h").is_file()
    for port, board in (("stm32", "OPENMV4P"), ("alif", "OPENMV_AE3"), ("mimxrt", "OPENMV_RT1060")):
        assert (stub_mod.STUB_DIR / "ports" / port / "port.c").is_file()
        assert (stub_mod.STUB_DIR / "boards" / board / "board.mk").is_file()


def test_compose_image_pads_the_stub_to_the_firmwares_destination():
    from openmv_ota.romfs.boards import StubLayout

    layout = StubLayout(0x70080000, 0x70090000, 0x36F000)
    img = stub_mod.compose_image(b"S" * 100, b"F" * 1000, layout)
    assert len(img) == 0x10000 + 1000
    assert img[:100] == b"S" * 100 and img[100:0x10000] == b"\xff" * (0x10000 - 100)
    assert img[0x10000:] == b"F" * 1000
    with pytest.raises(BuildError, match="stub is 65537 bytes but its sector holds 65536"):
        stub_mod.compose_image(b"S" * 65537, b"F", layout)


def test_combined_image_is_written_for_stub_boards_only(tmp_path):
    assert stub_mod.combined_image("OPENMV4", tmp_path) is None        # no stub layout
    with pytest.raises(BuildError, match="OPENMV_N6-stub.bin -- `build firmware` produces it"):
        stub_mod.combined_image("OPENMV_N6", tmp_path)
    (tmp_path / "OPENMV_N6-stub.bin").write_bytes(b"S" * 10)
    with pytest.raises(BuildError, match="OPENMV_N6-firmware.bin"):
        stub_mod.combined_image("OPENMV_N6", tmp_path)
    (tmp_path / "OPENMV_N6-firmware.bin").write_bytes(b"F" * 20)
    out = stub_mod.combined_image("OPENMV_N6", tmp_path)
    assert out == tmp_path / "OPENMV_N6-firmware-with-stub.bin"
    data = out.read_bytes()
    assert data[:10] == b"S" * 10 and data[0x10000:] == b"F" * 20 and len(data) == 0x10000 + 20


def test_combined_image_takes_the_boards_own_firmware_name(tmp_path):
    """The AE3's main image is firmware-M55_HP.bin (its flash.file map), its stub 32 KiB."""
    (tmp_path / "OPENMV_AE3-stub.bin").write_bytes(b"S" * 10)
    with pytest.raises(BuildError, match="OPENMV_AE3-firmware-M55_HP.bin"):
        stub_mod.combined_image("OPENMV_AE3", tmp_path)
    (tmp_path / "OPENMV_AE3-firmware-M55_HP.bin").write_bytes(b"H" * 20)
    data = stub_mod.combined_image("OPENMV_AE3", tmp_path).read_bytes()
    assert data[:10] == b"S" * 10 and data[0x8000:] == b"H" * 20 and len(data) == 0x8000 + 20
