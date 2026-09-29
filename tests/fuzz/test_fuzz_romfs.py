"""Fuzz the ROMFS container reader (``openmv_ota.romfs.container``) -- what ``romfs
inspect/verify/extract`` and ``build inspect`` run on an image file of unknown origin."""

from __future__ import annotations

import os
import tempfile

from hypothesis import given
from hypothesis import strategies as st

from openmv_ota.romfs.container import ROMFS_HEADER_MAGIC, RomfsError, VfsRomReader, VfsRomWriter

_names = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_.-", min_size=1,
                 max_size=12).filter(lambda s: s not in (".", ".."))

_tree = st.recursive(
    st.binary(max_size=80),
    lambda kids: st.dictionaries(_names, kids, max_size=4),
    max_leaves=10)


def _write(w, tree):
    for name, node in sorted(tree.items()):
        if isinstance(node, dict):
            w.opendir(name)
            _write(w, node)
            w.closedir()
        else:
            w.mkfile(name, node)


def _read(entries):
    return {e.name: (_read(e.children) if e.is_dir else e.data) for e in entries}


@given(st.dictionaries(_names, _tree, max_size=5),
       st.lists(st.fixed_dictionaries({"extension": st.sampled_from(["bin", "tflite", "py"]),
                                       "alignment": st.sampled_from([4, 8, 16, 32, 64])}),
                max_size=3))
def test_write_then_read_round_trips_with_alignment(tree, rules):
    w = VfsRomWriter(rules)
    _write(w, tree)
    img = w.finalize()
    r = VfsRomReader(img + b"\xff" * 7)          # bytes past the romfs (a trailer) are ignored
    assert _read(r.entries) == tree
    assert r.romfs_size == len(img)


def _parse(data):
    try:
        return VfsRomReader(data)
    except RomfsError:
        return None


_hostile_names = st.sampled_from(["..", ".", "", "../x", "a/../../b", "/abs", "ok"]) | st.text(
    max_size=6)


@st.composite
def hostile_images(draw):
    """Well-formed images (so the fuzzer gets past framing) with hostile names and nesting,
    or raw bytes behind a valid magic."""
    if draw(st.booleans()):
        return ROMFS_HEADER_MAGIC + draw(st.binary(max_size=400))
    w = VfsRomWriter()
    depth = draw(st.integers(0, 3))
    for _ in range(depth):
        w.opendir(draw(_hostile_names))
    for _ in range(draw(st.integers(0, 3))):
        w.mkfile(draw(_hostile_names), draw(st.binary(max_size=16)))
    for _ in range(depth):
        w.closedir()
    return w.finalize()


@given(st.binary(max_size=3000) | hostile_images())
def test_arbitrary_images_parse_or_raise_and_never_extract_outside_dest(data):
    r = _parse(data)
    if r is None:
        return
    list(r.walk())
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "out")
        os.mkdir(dest)
        try:
            r.extract(dest)
        except (RomfsError, OSError):              # OSError: a name the filesystem refuses
            pass
        real = os.path.realpath(dest) + os.sep
        for root, dirs, files in os.walk(d):
            for f in dirs + files:
                path = os.path.realpath(os.path.join(root, f))
                assert path + os.sep == real or path.startswith(real), path
