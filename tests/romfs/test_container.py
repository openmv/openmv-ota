"""Tests for the ROMFS container format (writer + reader)."""

from __future__ import annotations

import pytest

from openmv_ota.romfs import container as c


# --- encode_uint ------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (0x7F, b"\x7f"),
        (0x80, b"\x81\x00"),
        (0x3FFF, b"\xff\x7f"),
        (0x4000, b"\x81\x80\x00"),
    ],
)
def test_encode_uint(value, expected):
    assert c.encode_uint(value) == expected


def test_encode_uint_negative():
    with pytest.raises(ValueError):
        c.encode_uint(-1)


def test_header_magic_constants():
    # 0x80|'R', 0x80|'M', '1'
    assert c.ROMFS_HEADER_MAGIC == bytes((0xD2, 0xCD, 0x31))
    # The header "kind" encodes back to exactly the three magic bytes.
    assert c.encode_uint(c.ROMFS_HEADER_KIND) == c.ROMFS_HEADER_MAGIC


# --- complete_suffix / alignment_for ----------------------------------------

@pytest.mark.parametrize(
    "name, suffix",
    [
        ("model.tflite", "tflite"),
        ("archive.tar.gz", "tar.gz"),
        ("main.py", "py"),
        ("noext", ""),
        (".gitignore", ""),       # leading dot is not a separator
        ("UPPER.TFLITE", "tflite"),
    ],
)
def test_complete_suffix(name, suffix):
    assert c.complete_suffix(name) == suffix


def test_alignment_for_matches_rule_case_insensitively():
    rules = [{"extension": "tflite", "alignment": 32}, {"extension": "bin", "alignment": 16}]
    assert c.alignment_for("net.TFLITE", rules) == 32
    assert c.alignment_for("blob.bin", rules) == 16
    assert c.alignment_for("main.py", rules) == c.ROMFS_MIN_ALIGNMENT


def test_alignment_for_custom_default():
    assert c.alignment_for("main.py", [], default=64) == 64


# --- to_ascii ---------------------------------------------------------------

def test_to_ascii_replaces_non_printable():
    # Control char and a non-ASCII Latin-1 char both become '?'.
    assert c.to_ascii("a\x01b") == b"a?b"
    assert c.to_ascii("café") == b"caf?"
    # A character outside Latin-1 is replaced during encoding, then normalised.
    assert c.to_ascii("sn☃w") == b"sn?w"


# --- writer / reader round-trip ---------------------------------------------

def _build(tree, rules=None):
    """tree: nested dict; str/bytes leaf = file, dict = directory."""
    w = c.VfsRomWriter(rules or [])

    def emit(node):
        for name in sorted(node):
            val = node[name]
            if isinstance(val, dict):
                w.opendir(name)
                emit(val)
                w.closedir()
            else:
                w.mkfile(name, val if isinstance(val, bytes) else val.encode())

    emit(tree)
    return w.finalize()


def test_roundtrip_simple():
    tree = {"main.py": "print(1)", "data.bin": b"\x00\x01\x02"}
    img = _build(tree)
    assert img[:3] == c.ROMFS_HEADER_MAGIC

    r = c.VfsRomReader(img)
    got = {p: e.data for p, e in r.walk() if not e.is_dir}
    assert got == {"data.bin": b"\x00\x01\x02", "main.py": b"print(1)"}


def test_roundtrip_nested():
    tree = {
        "main.py": "m",
        "lib": {"a.py": "a", "sub": {"b.py": "b"}},
        "models": {"net.tflite": b"M" * 50},
    }
    img = _build(tree, rules=[{"extension": "tflite", "alignment": 32}])
    r = c.VfsRomReader(img)
    got = {p: (e.is_dir, e.data) for p, e in r.walk()}
    assert got["lib"][0] is True
    assert got["lib/sub"][0] is True
    assert got["lib/sub/b.py"] == (False, b"b")
    assert got["models/net.tflite"] == (False, b"M" * 50)


def test_empty_directory_roundtrips():
    img = _build({"empty": {}, "f.txt": "x"})
    r = c.VfsRomReader(img)
    names = {p: e.is_dir for p, e in r.walk()}
    assert names == {"empty": True, "f.txt": False}


# --- alignment: payloads land on the requested boundary ---------------------

def _payload_offsets(img):
    """Return {path: (suffix-payload absolute offset)} by re-parsing the image."""
    data = img
    pos = 0

    def du(p):
        v = 0
        while True:
            b = data[p]
            p += 1
            v = (v << 7) | (b & 0x7F)
            if not (b & 0x80):
                return v, p

    _, pos = du(pos)
    size, pos = du(pos)
    offsets = {}

    def parse(start, stop, prefix=""):
        p = start
        while p < stop:
            kind, p2 = du(p)
            length, p3 = du(p2)
            body, nxt = p3, p3 + length
            if kind in (c.ROMFS_RECORD_KIND_DIRECTORY, c.ROMFS_RECORD_KIND_FILE):
                nl, b = du(body)
                name = data[b:b + nl].decode("latin-1")
                b += nl
                path = prefix + "/" + name if prefix else name
                if kind == c.ROMFS_RECORD_KIND_DIRECTORY:
                    parse(b, nxt, path)
                else:
                    _, db = du(b)         # data kind
                    _, db2 = du(db)       # data length
                    offsets[path] = db2
            p = nxt

    parse(pos, min(pos + size, len(data)))
    return offsets


@pytest.mark.parametrize("alignment", [16, 32, 64, 128])
def test_payload_alignment(alignment):
    rules = [{"extension": "bin", "alignment": alignment}]
    tree = {
        "a.txt": "x" * 3,
        "m.bin": b"\xaa" * 200,
        "lib": {"n.bin": b"\xbb" * 100, "s.py": "s"},
    }
    img = _build(tree, rules)
    offs = _payload_offsets(img)
    assert offs["m.bin"] % alignment == 0
    assert offs["lib/n.bin"] % alignment == 0


# --- determinism ------------------------------------------------------------

def test_deterministic_output():
    tree = {"b.py": "b", "a.py": "a", "d": {"y.bin": b"y", "x.bin": b"x"}}
    rules = [{"extension": "bin", "alignment": 16}]
    assert _build(tree, rules) == _build(tree, rules)


# --- malformed input --------------------------------------------------------

def test_reader_rejects_short():
    with pytest.raises(c.RomfsError):
        c.VfsRomReader(b"\x00")


def test_reader_rejects_bad_magic():
    with pytest.raises(c.RomfsError):
        c.VfsRomReader(b"\xde\xad\xbe\xef\x00")


def test_writer_unbalanced_dirs():
    w = c.VfsRomWriter([])
    w.opendir("x")
    with pytest.raises(RuntimeError):
        w.finalize()
    w2 = c.VfsRomWriter([])
    with pytest.raises(RuntimeError):
        w2.closedir()


def test_writer_finalize_two_unclosed_dirs():
    w = c.VfsRomWriter([])
    w.opendir("a")
    w.opendir("b")
    with pytest.raises(RuntimeError):
        w.finalize()


def test_reader_truncated_integer():
    # Valid magic, then a lone continuation byte and EOF -> truncated size.
    with pytest.raises(c.RomfsError):
        c.VfsRomReader(c.ROMFS_HEADER_MAGIC + b"\x80")


# --- crafted reader vectors: DATA_POINTER and unknown data kinds ------------

def _wrap_root(root: bytes) -> bytes:
    eu = c.encode_uint
    return eu(c.ROMFS_HEADER_KIND) + eu(len(root)) + root


def _file_record(name: str, datakind: int, data_payload: bytes) -> bytes:
    eu = c.encode_uint
    nb = c.to_ascii(name)
    name_rec = eu(len(nb)) + nb
    data_rec = eu(datakind) + eu(len(data_payload)) + data_payload
    body = name_rec + data_rec
    return eu(c.ROMFS_RECORD_KIND_FILE) + eu(len(body)) + body


def test_reader_data_pointer():
    blob = b"HELLO-POINTER-PAYLOAD"
    eu = c.encode_uint

    def assemble(dp_data: int) -> bytes:
        two = eu(len(blob)) + eu(dp_data)
        payload = two + b"\x00" * max(0, 8 - len(two))  # reader needs payloadlen >= 8
        root = _file_record("a.bin", c.ROMFS_RECORD_KIND_DATA_POINTER, payload)
        return _wrap_root(root)

    head = assemble(0)
    dp_data = len(head)            # blob is appended right after the header record
    image = assemble(dp_data) + blob
    assert len(assemble(dp_data)) == len(head)  # offset width stayed stable

    reader = c.VfsRomReader(image)
    files = {p: e for p, e in reader.walk() if not e.is_dir}
    assert files["a.bin"].data == blob
    assert files["a.bin"].data_offset == dp_data


def test_reader_unknown_data_kind_yields_empty():
    root = _file_record("weird.dat", 99, b"\x01\x02\x03")
    reader = c.VfsRomReader(_wrap_root(root))
    files = {p: e for p, e in reader.walk() if not e.is_dir}
    assert files["weird.dat"].data == b""
