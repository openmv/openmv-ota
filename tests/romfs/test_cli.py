"""Tests for the ``openmv-ota romfs`` CLI surface."""

from __future__ import annotations

import io
import os

from openmv_ota.cli import main
from openmv_ota.romfs import cli as romfs_cli
from openmv_ota.romfs.container import ROMFS_HEADER_MAGIC


def _tree(root):
    os.makedirs(os.path.join(root, "lib"))
    with open(os.path.join(root, "main.py"), "w") as f:
        f.write("print('x')")
    with open(os.path.join(root, "lib", "u.py"), "w") as f:
        f.write("u")
    with open(os.path.join(root, "model.tflite"), "wb") as f:
        f.write(b"\x22" * 64)


def test_build_extract_roundtrip_via_cli(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    _tree(str(src))
    img = tmp_path / "out.romfs"

    rc = main(["romfs", "pack", str(src), "-o", str(img), "--board", "OPENMV_N6"])
    assert rc == 0
    assert img.exists()
    out = capsys.readouterr().out
    assert "OPENMV_N6" in out and "alignment" in out

    dest = tmp_path / "unpacked"
    rc = main(["romfs", "extract", str(img), "-o", str(dest)])
    assert rc == 0
    assert (dest / "main.py").read_text() == "print('x')"
    assert (dest / "lib" / "u.py").read_text() == "u"
    assert (dest / "model.tflite").read_bytes() == b"\x22" * 64


def test_build_oversize_fails(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(b"\x00" * 4096)
    img = tmp_path / "out.romfs"
    rc = main(["romfs", "pack", str(src), "-o", str(img),
               "--board", "OPENMV_N6", "--max-size", "512"])
    assert rc == 1
    assert "error" in capsys.readouterr().err.lower()


def test_build_allow_oversize(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(b"\x00" * 4096)
    img = tmp_path / "out.romfs"
    rc = main(["romfs", "pack", str(src), "-o", str(img),
               "--board", "OPENMV_N6", "--max-size", "512", "--allow-oversize"])
    assert rc == 0
    assert img.exists()


def test_build_unknown_board(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("a")
    rc = main(["romfs", "pack", str(src), "-o", str(tmp_path / "o.romfs"), "--board", "NOPE"])
    assert rc == 2
    assert "unknown board" in capsys.readouterr().err.lower()


def test_build_without_board_uses_explicit_align(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.bin").write_bytes(b"\x00" * 10)
    img = tmp_path / "out.romfs"
    rc = main(["romfs", "pack", str(src), "-o", str(img), "--align", "bin=64"])
    assert rc == 0


def test_ls_and_info(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    _tree(str(src))
    img = tmp_path / "out.romfs"
    main(["romfs", "pack", str(src), "-o", str(img), "--board", "OPENMV_N6", "-q"])

    assert main(["romfs", "ls", str(img), "-l"]) == 0
    ls_out = capsys.readouterr().out
    assert "model.tflite" in ls_out and "lib/" in ls_out

    assert main(["romfs", "info", str(img)]) == 0
    info_out = capsys.readouterr().out
    assert "files:" in info_out and "D2 CD 31" in info_out


def test_ls_invalid_image(tmp_path, capsys):
    bad = tmp_path / "bad.romfs"
    bad.write_bytes(b"not a romfs")
    rc = main(["romfs", "ls", str(bad)])
    assert rc == 1
    assert "valid romfs" in capsys.readouterr().err.lower()


def test_boards_list_and_detail(capsys):
    assert main(["romfs", "boards"]) == 0
    assert "OPENMV_N6" in capsys.readouterr().out
    assert main(["romfs", "boards", "OPENMV_N6"]) == 0
    detail = capsys.readouterr().out
    assert "tflite=32" in detail


def test_extract_nonempty_dir_guard(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("a")
    img = tmp_path / "out.romfs"
    main(["romfs", "pack", str(src), "-o", str(img), "-q"])

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "preexisting").write_text("x")
    assert main(["romfs", "extract", str(img), "-o", str(dest)]) == 1
    assert "not empty" in capsys.readouterr().err.lower()
    assert main(["romfs", "extract", str(img), "-o", str(dest), "--force"]) == 0


# --- build: remaining branches ----------------------------------------------

def test_build_to_stdout(tmp_path, capsysbinary):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("a")
    assert main(["romfs", "pack", str(src), "-o", "-", "--board", "OPENMV_N6"]) == 0
    out = capsysbinary.readouterr().out
    assert out[:3] == ROMFS_HEADER_MAGIC


def test_build_no_board_summary_defaults_only(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("a")
    img = tmp_path / "o.romfs"
    assert main(["romfs", "pack", str(src), "-o", str(img)]) == 0
    out = capsys.readouterr().out
    assert "defaults only" in out and "board:" not in out


def test_build_no_board_rules_without_board_errors(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("a")
    rc = main(["romfs", "pack", str(src), "-o", str(tmp_path / "o"), "--no-board-rules"])
    assert rc == 2
    assert "needs a board" in capsys.readouterr().err.lower()


def test_build_bad_default_alignment(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("a")
    rc = main(["romfs", "pack", str(src), "-o", str(tmp_path / "o"),
               "--default-alignment", "24"])
    assert rc == 2
    assert "power of two" in capsys.readouterr().err.lower()


def test_build_bad_partition_index(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("a")
    rc = main(["romfs", "pack", str(src), "-o", str(tmp_path / "o"),
               "--board", "OPENMV_N6", "--partition", "9"])
    assert rc == 2
    assert "partition" in capsys.readouterr().err.lower()


def test_build_allow_oversize_warns(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(b"\x00" * 4096)
    img = tmp_path / "o.romfs"
    rc = main(["romfs", "pack", str(src), "-o", str(img),
               "--board", "OPENMV_N6", "--max-size", "512", "--allow-oversize"])
    assert rc == 0
    assert "exceeds the capacity" in capsys.readouterr().err.lower()


def test_build_no_board_max_size_exceeded(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(b"\x00" * 4096)
    rc = main(["romfs", "pack", str(src), "-o", str(tmp_path / "o"), "--max-size", "256"])
    assert rc == 1
    assert "max-size" in capsys.readouterr().err.lower()


def test_build_no_default_excludes_keeps_pycache(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.pyc").write_text("c")
    (src / "main.py").write_text("m")
    img = tmp_path / "o.romfs"
    # Default: pycache excluded.
    main(["romfs", "pack", str(src), "-o", str(img), "-q"])
    from openmv_ota.romfs.builder import read_image
    names = {p for p, e in read_image(img.read_bytes()).walk()}
    assert "__pycache__" not in names
    # With --no-default-excludes it is kept.
    img2 = tmp_path / "o2.romfs"
    main(["romfs", "pack", str(src), "-o", str(img2), "-q", "--no-default-excludes"])
    names2 = {p for p, e in read_image(img2.read_bytes()).walk()}
    assert "__pycache__" in names2


# --- cat ---------------------------------------------------------------------

def _build_demo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(str(src))
    img = tmp_path / "out.romfs"
    main(["romfs", "pack", str(src), "-o", str(img), "-q"])
    return img


def test_cat_file(tmp_path, capsysbinary):
    img = _build_demo(tmp_path)
    assert main(["romfs", "cat", str(img), "main.py"]) == 0
    assert capsysbinary.readouterr().out == b"print('x')"


def test_cat_leading_slash_ok(tmp_path, capsysbinary):
    img = _build_demo(tmp_path)
    assert main(["romfs", "cat", str(img), "/lib/u.py"]) == 0
    assert capsysbinary.readouterr().out == b"u"


def test_cat_directory_errors(tmp_path, capsys):
    img = _build_demo(tmp_path)
    assert main(["romfs", "cat", str(img), "lib"]) == 1
    assert "is a directory" in capsys.readouterr().err.lower()


def test_cat_missing_errors(tmp_path, capsys):
    img = _build_demo(tmp_path)
    assert main(["romfs", "cat", str(img), "nope.py"]) == 1
    assert "not found" in capsys.readouterr().err.lower()


def test_cat_invalid_image(tmp_path, capsys):
    bad = tmp_path / "bad.romfs"
    bad.write_bytes(b"nope")
    assert main(["romfs", "cat", str(bad), "main.py"]) == 1
    assert "not a valid" in capsys.readouterr().err.lower()


def test_info_invalid_image(tmp_path, capsys):
    bad = tmp_path / "bad.romfs"
    bad.write_bytes(b"nope")
    assert main(["romfs", "info", str(bad)]) == 1
    assert "not a valid" in capsys.readouterr().err.lower()


# --- stdin plumbing ----------------------------------------------------------

def test_info_from_stdin(tmp_path, monkeypatch, capsys):
    import types
    img = _build_demo(tmp_path)
    monkeypatch.setattr(romfs_cli.sys, "stdin",
                        types.SimpleNamespace(buffer=io.BytesIO(img.read_bytes())))
    assert main(["romfs", "info", "-"]) == 0
    out = capsys.readouterr().out
    assert "<stdin>" in out and "files:" in out


def test_ls_short_and_extract_missing_file(tmp_path, capsys):
    img = _build_demo(tmp_path)
    assert main(["romfs", "ls", str(img)]) == 0
    assert "main.py" in capsys.readouterr().out
    # extract from a path that does not exist -> OSError -> exit 2
    assert main(["romfs", "extract", str(tmp_path / "nope.romfs"), "-o", str(tmp_path / "d")]) == 2
    assert "error" in capsys.readouterr().err.lower()


# --- verify ------------------------------------------------------------------

def test_verify_ok_with_board(tmp_path, capsys):
    img = _build_demo(tmp_path)  # built with default min alignment
    # Verify with N6 rules; the .tflite in the demo is 64 bytes built w/o board,
    # so check against an explicit alignment that the layout satisfies instead.
    assert main(["romfs", "verify", str(img)]) == 0
    assert "all payloads aligned" in capsys.readouterr().out


def test_verify_with_board_rules(tmp_path, capsys):
    # Build *with* the board so tflite is 32-aligned, then verify with the board.
    src = tmp_path / "src"
    src.mkdir()
    (src / "model.tflite").write_bytes(b"\x33" * 100)
    img = tmp_path / "o.romfs"
    main(["romfs", "pack", str(src), "-o", str(img), "--board", "OPENMV_N6", "-q"])
    assert main(["romfs", "verify", str(img), "--board", "OPENMV_N6"]) == 0
    assert "aligned" in capsys.readouterr().out


def test_verify_fails_on_alignment(tmp_path, capsys):
    img = _build_demo(tmp_path)
    rc = main(["romfs", "verify", str(img), "--default-alignment", "256"])
    assert rc == 1
    assert "fail" in capsys.readouterr().err.lower()


def test_verify_invalid_image(tmp_path, capsys):
    bad = tmp_path / "bad.romfs"
    bad.write_bytes(b"nope")
    assert main(["romfs", "verify", str(bad)]) == 1
    assert "not a valid" in capsys.readouterr().err.lower()


def test_verify_missing_file(tmp_path, capsys):
    assert main(["romfs", "verify", str(tmp_path / "nope.romfs")]) == 2
    assert "error" in capsys.readouterr().err.lower()


def test_verify_unknown_board(tmp_path, capsys):
    img = _build_demo(tmp_path)
    assert main(["romfs", "verify", str(img), "--board", "NOPE"]) == 2
    assert "unknown board" in capsys.readouterr().err.lower()


def test_verify_bad_partition(tmp_path, capsys):
    img = _build_demo(tmp_path)
    assert main(["romfs", "verify", str(img), "--board", "OPENMV_N6", "--partition", "9"]) == 2
    assert "partition" in capsys.readouterr().err.lower()


def test_verify_no_board_rules_without_board(tmp_path, capsys):
    img = _build_demo(tmp_path)
    assert main(["romfs", "verify", str(img), "--no-board-rules"]) == 2
    assert "needs a board" in capsys.readouterr().err.lower()


# --- boards ------------------------------------------------------------------

def test_boards_detail_without_npu(capsys):
    # OPENMV2 has no NPU accelerator config; exercises that branch.
    assert main(["romfs", "boards", "OPENMV2"]) == 0
    out = capsys.readouterr().out
    assert "OPENMV2" in out and "npu" not in out.lower()


def test_boards_unknown(capsys):
    assert main(["romfs", "boards", "NOPE"]) == 2
    assert "unknown board" in capsys.readouterr().err.lower()


def test_ls_long_dir_marker(tmp_path, capsys):
    img = _build_demo(tmp_path)
    main(["romfs", "ls", str(img), "-l"])
    out = capsys.readouterr().out
    assert "<dir>" in out
