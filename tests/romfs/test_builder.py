"""Tests for the directory-tree builder and capacity handling."""

from __future__ import annotations

import os

import pytest

from openmv_ota.romfs import boards as boards_mod
from openmv_ota.romfs import builder as b
from openmv_ota.romfs.container import VfsRomReader


def _make_tree(root):
    os.makedirs(os.path.join(root, "lib"))
    os.makedirs(os.path.join(root, "models"))
    with open(os.path.join(root, "main.py"), "w") as f:
        f.write("print('hi')")
    with open(os.path.join(root, "lib", "util.py"), "w") as f:
        f.write("util")
    with open(os.path.join(root, "models", "net.tflite"), "wb") as f:
        f.write(b"\x11" * 300)


def test_build_image_roundtrip(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(str(src))
    img = b.build_image(str(src), [{"extension": "tflite", "alignment": 32}])

    r = VfsRomReader(img)
    got = {p: e.data for p, e in r.walk() if not e.is_dir}
    assert got == {
        "main.py": b"print('hi')",
        "lib/util.py": b"util",
        "models/net.tflite": b"\x11" * 300,
    }


def test_build_image_is_deterministic(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(str(src))
    assert b.build_image(str(src)) == b.build_image(str(src))


def test_build_image_not_a_directory(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("nope")
    with pytest.raises(b.BuildError):
        b.build_image(str(f))


def test_build_for_board_capacity_ok(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(str(src))
    board = boards_mod.get_board("OPENMV_N6")
    res = b.build_for_board(str(src), board)
    assert res.partition.index == 0
    assert res.size == len(res.image)
    assert res.free == res.capacity - res.size
    # tflite alignment for N6 is 32.
    assert any(r["extension"] == "tflite" and r["alignment"] == 32 for r in res.alignment_rules)


def test_build_for_board_oversize_raises(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(b"\x00" * 4096)
    board = boards_mod.get_board("OPENMV_N6")
    with pytest.raises(b.BuildError):
        b.build_for_board(str(src), board, max_size=512)
    # allow_oversize converts the error into a successful (over-capacity) build.
    res = b.build_for_board(str(src), board, max_size=512, allow_oversize=True)
    assert res.size > 512


def test_build_for_board_extra_rules_override(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.bin").write_bytes(b"\x00" * 10)
    board = boards_mod.get_board("OPENMV_N6")  # bin=32 by default
    res = b.build_for_board(str(src), board, extra_rules=[{"extension": "bin", "alignment": 64}])
    bin_rule = [r for r in res.alignment_rules if r["extension"] == "bin"]
    assert bin_rule == [{"extension": "bin", "alignment": 64}]


def test_build_partition_selection(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("x")
    board = boards_mod.get_board("OPENMV_AE3")  # two partitions: 0 and 1
    r0 = b.build_for_board(str(src), board, partition_index=0)
    r1 = b.build_for_board(str(src), board, partition_index=1)
    assert r0.partition.index == 0 and r1.partition.index == 1
    assert r0.capacity != r1.capacity


def test_excludes(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep.py").write_text("k")
    (src / "skip.log").write_text("s")
    (src / "junk").mkdir()
    (src / "junk" / "x.py").write_text("x")
    img = b.build_image(str(src), exclude=["*.log", "junk"])
    names = {p for p, e in VfsRomReader(img).walk()}
    assert names == {"keep.py"}


def test_default_excludes_constant_covers_pycache():
    assert "__pycache__" in b.DEFAULT_EXCLUDES
    assert "*.pyc" in b.DEFAULT_EXCLUDES


def test_default_alignment(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.unknown").write_bytes(b"\xee" * 40)
    # With a 64-byte default, an unmatched extension still aligns to 64.
    img = b.build_image(str(src), default_alignment=64)
    res = b.verify_image(img, [], default_alignment=64)
    assert res.ok


def test_symlinks_skipped_and_followed(tmp_path, monkeypatch):
    # Drive the symlink branch deterministically with a fake DirEntry, so the
    # test is meaningful even where real symlinks need privileges (Windows).
    real = tmp_path / "src"
    real.mkdir()
    (real / "real.txt").write_text("r")
    target = real / "real.txt"

    class FakeEntry:
        def __init__(self, name, path, is_link):
            self.name = name
            self.path = path
            self._is_link = is_link

        def is_symlink(self):
            return self._is_link

        def is_dir(self):
            return False

        def is_file(self):
            return True

    fake = [
        FakeEntry("real.txt", str(target), False),
        FakeEntry("link.txt", str(target), True),
    ]
    monkeypatch.setattr(b.os, "scandir", lambda p: iter(fake))

    skipped = b.build_image(str(real))
    names = {p for p, e in VfsRomReader(skipped).walk()}
    assert "real.txt" in names and "link.txt" not in names

    followed = b.build_image(str(real), follow_symlinks=True)
    names = {p for p, e in VfsRomReader(followed).walk()}
    assert {"real.txt", "link.txt"} <= names


def test_build_result_without_partition():
    res = b.BuildResult(image=b"x" * 10, partition=None, alignment_rules=[])
    assert res.size == 10
    assert res.capacity is None
    assert res.free is None


def test_resolve_rules_modes():
    board = boards_mod.get_board("OPENMV_N6")
    p = board.partition(0)
    # Board rules only.
    base = b.resolve_rules(p)
    assert any(r["extension"] == "tflite" for r in base)
    # Disable board rules.
    assert b.resolve_rules(p, use_board_rules=False) == []
    # Extra overrides board.
    overridden = b.resolve_rules(p, extra_rules=[{"extension": "tflite", "alignment": 8}])
    assert {"extension": "tflite", "alignment": 8} in overridden


def test_merge_rules_dedupes_and_lowercases():
    merged = b.merge_rules(
        [{"extension": "BIN", "alignment": 16}],
        [{"extension": "bin", "alignment": 64}, {"extension": "tflite", "alignment": 32}],
    )
    by_ext = {r["extension"]: r["alignment"] for r in merged}
    assert by_ext == {"bin": 64, "tflite": 32}


def test_verify_image_ok_and_problems(tmp_path):
    rules = [{"extension": "bin", "alignment": 16}]
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.bin").write_bytes(b"\x00" * 50)
    (src / "sub").mkdir()
    (src / "sub" / "b.bin").write_bytes(b"\x11" * 32)
    img = b.build_image(str(src), rules)

    ok = b.verify_image(img, rules)
    assert ok.ok and ok.files == 2 and ok.dirs == 1

    bad = b.verify_image(img, [{"extension": "bin", "alignment": 256}])
    assert not bad.ok and bad.problems


def test_verify_image_rejects_garbage():
    from openmv_ota.romfs.container import RomfsError
    with pytest.raises(RomfsError):
        b.verify_image(b"not romfs")
