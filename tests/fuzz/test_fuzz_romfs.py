"""Fuzz the ROMFS container reader (``openmv_ota.romfs.container``) -- what ``romfs
inspect/verify/extract`` and ``build inspect`` run on an image file of unknown origin."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from openmv_ota.romfs.container import VfsRomReader, VfsRomWriter

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
