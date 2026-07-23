"""Host tests for the device OTA installer's pure logic.

The installer ships in the romfs as source and is ``exec``'d into RAM on-device, so
here it's loaded the same way -- read + ``exec`` at its real path -- which both
exercises the pure helpers and lets coverage measure the file. The device entry
points (``run`` / ``_open`` / ``_connect``) need socket/ssl/vfs and are
``pragma: no cover`` (the QEMU suite drives the exec-into-RAM + clean-failure path).
"""

import importlib.util
from pathlib import Path

import pytest

# Load the installer the way coverage can measure it: a real file-based import (raw
# exec of the source isn't tracked because data/ is not a package). On-device the same
# file is read + exec'd into RAM by openmv_ota.install().
_SRC = (Path(__file__).resolve().parents[2]
        / "src/openmv_ota/build/device/openmv_ota/data/installer.py")
# The dotted name must live under "openmv_ota" so --cov=openmv_ota measures the file.
_spec = importlib.util.spec_from_file_location("openmv_ota._installer_under_test", str(_SRC))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def inst(name):
    return getattr(_mod, name)


# --- PENDING marker is pinned to the canonical one --------------------------

def test_pending_marker_matches_status_module():
    from openmv_ota.ota import status
    assert inst("PENDING") == status.PENDING
    assert len(inst("PENDING")) == inst("MARKER_SIZE") == 16
    assert (inst("REPR_FULL"), inst("REPR_DELTA")) == (status.REPR_FULL, status.REPR_DELTA)
    assert inst("_REPR_OFF") == status.REPR_OFFSET


def test_install_stream_writes_repr_marker():
    from openmv_ota.ota import status
    block, front = 4096, 3 * 4096
    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"
    so = front - 2 * block                                # status sector base
    ro = so + status.REPR_OFFSET
    flash = _run_install(bytes(image), front, block, repr_marker=inst("REPR_DELTA"))
    assert flash.mem[so:so + 16] == inst("PENDING")       # armed
    assert flash.mem[ro:ro + 16] == status.REPR_DELTA     # rep recorded beside pending
    # no repr_marker -> the slot has none (a factory image looks like this)
    flash2 = _run_install(bytes(image), front, block)
    assert flash2.mem[ro:ro + 16] == b"\xff" * 16


# --- _parse_url -------------------------------------------------------------

@pytest.mark.parametrize(("url", "expect"), [
    ("https://example.com/a/b.gz", ("example.com", 443, "/a/b.gz")),
    ("https://example.com", ("example.com", 443, "/")),
    ("https://h:8443/x", ("h", 8443, "/x")),
    ("https://pub.r2.dev/o.img.gz?X-Amz-Sig=abc&y=1",
     ("pub.r2.dev", 443, "/o.img.gz?X-Amz-Sig=abc&y=1")),  # query preserved
])
def test_parse_url_ok(url, expect):
    assert inst("_parse_url")(url) == expect


@pytest.mark.parametrize("url", [
    "http://example.com/x",        # plaintext refused
    "ftp://example.com/x",
    "example.com/x",
    "https://:443/x",              # no host
    "https://h:notaport/x",       # bad port
])
def test_parse_url_rejects(url):
    with pytest.raises(ValueError):
        inst("_parse_url")(url)


@pytest.mark.parametrize(("manifest_url", "rep_url", "expect"), [
    # relative filename -> resolved against the manifest's own URL (the default)
    ("https://dl.x.io/fw/N6-manifest.bin", "N6-ota.img.gz",
     "https://dl.x.io/fw/N6-ota.img.gz"),
    ("https://dl.x.io/fw/N6-manifest.bin", "./N6-ota.delta.gz",
     "https://dl.x.io/fw/N6-ota.delta.gz"),
    # absolute https -> used as-is (an off-host CDN)
    ("https://dl.x.io/fw/N6-manifest.bin", "https://cdn.y.io/a.gz",
     "https://cdn.y.io/a.gz"),
])
def test_resolve_url(manifest_url, rep_url, expect):
    assert inst("_resolve_url")(manifest_url, rep_url) == expect


# --- request line + status + small parsers ----------------------------------

def test_request_bytes():
    req = inst("_request_bytes")("h.io", 443, "/o.gz")
    assert req.startswith(b"GET /o.gz HTTP/1.1\r\n")
    assert b"Host: h.io\r\n" in req
    assert b"Connection: close\r\n" in req and req.endswith(b"\r\n\r\n")


def test_request_bytes_nondefault_port_in_host():
    assert b"Host: h.io:8443\r\n" in inst("_request_bytes")("h.io", 8443, "/o")


@pytest.mark.parametrize(("line", "code"), [
    (b"HTTP/1.1 200 OK\r\n", 200), (b"HTTP/1.0 301 Moved\r\n", 301),
    (b"HTTP/1.1 404 Not Found", 404)])
def test_parse_status_ok(line, code):
    assert inst("_parse_status")(line) == code


@pytest.mark.parametrize("line", [b"\r\n", b"garbage\r\n", b"HTTP/1.1 nope OK\r\n"])
def test_parse_status_bad(line):
    with pytest.raises(ValueError):
        inst("_parse_status")(line)


@pytest.mark.parametrize(("code", "is_r"), [
    (301, True), (302, True), (303, True), (307, True), (308, True),
    (200, False), (304, False), (400, False)])
def test_is_redirect(code, is_r):
    assert inst("_is_redirect")(code) is is_r


@pytest.mark.parametrize(("line", "size"), [
    (b"1a\r\n", 0x1a), (b"1A3F\r\n", 0x1a3f), (b"ff;name=x\r\n", 0xff), (b"0\r\n", 0)])
def test_chunk_size_ok(line, size):
    assert inst("_chunk_size")(line) == size


def test_chunk_size_empty():
    with pytest.raises(ValueError):
        inst("_chunk_size")(b";ext\r\n")


def test_is_blank():
    assert inst("_is_blank")(b"\xff" * 8) is True
    assert inst("_is_blank")(b"\xff\x00\xff") is False
    assert inst("_is_blank")(b"") is True


# --- _Reader ----------------------------------------------------------------

def _recv_of(*pieces):
    """A recv(n) callable that hands back successive pieces, then EOF."""
    it = iter(pieces)

    def recv(_n):
        return next(it, b"")
    return recv


def test_reader_readline_across_recvs():
    r = inst("_Reader")(_recv_of(b"HTTP/1.1 ", b"200 OK\r\nX: 1\r\n\r\n"))
    assert r.readline() == b"HTTP/1.1 200 OK\r\n"
    assert r.readline() == b"X: 1\r\n"
    assert r.readline() == b"\r\n"


def test_reader_readline_eof_without_newline():
    r = inst("_Reader")(_recv_of(b"tail-no-newline"))
    assert r.readline() == b"tail-no-newline"
    assert r.readline() == b""


def test_reader_readline_too_long():
    r = inst("_Reader")(_recv_of(b"x" * 9000))
    with pytest.raises(ValueError):
        r.readline(limit=8192)


def test_reader_read_exact_and_some():
    r = inst("_Reader")(_recv_of(b"abcdef", b"ghij"))
    assert r.read_exact(4) == b"abcd"
    assert r.read_some(100) == b"ef"      # only the buffered remainder
    assert r.read_exact(4) == b"ghij"


def test_reader_read_exact_eof():
    r = inst("_Reader")(_recv_of(b"ab"))
    with pytest.raises(ValueError):
        r.read_exact(4)


def test_reader_read_some_eof_returns_empty():
    r = inst("_Reader")(_recv_of())
    assert r.read_some(10) == b""


# --- _read_response ---------------------------------------------------------

def test_read_response():
    raw = (b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n"
           b"Transfer-Encoding: chunked\r\nLocation: /x\r\n\r\nBODY!")
    r = inst("_Reader")(_recv_of(raw))
    code, headers = inst("_read_response")(r)
    assert code == 200
    assert headers[b"content-length"] == b"5"
    assert headers[b"transfer-encoding"] == b"chunked"
    assert headers[b"location"] == b"/x"
    assert r.read_some(10) == b"BODY!"     # positioned at the body


def test_read_response_ignores_non_header_line():
    raw = b"HTTP/1.1 204 No Content\r\nnocolonhere\r\n\r\n"
    code, headers = inst("_read_response")(inst("_Reader")(_recv_of(raw)))
    assert code == 204 and headers == {}


# --- _Body / _make_body -----------------------------------------------------

def _drain(body, n=3):
    """Read a _Body to EOF via readinto, n bytes at a time."""
    out = bytearray()
    while True:
        buf = bytearray(n)
        got = body.readinto(buf)
        if got == 0:
            return bytes(out)
        out += buf[:got]


def test_body_content_length():
    r = inst("_Reader")(_recv_of(b"HELLOworld-extra"))
    body = inst("_make_body")(r, {b"content-length": b"5"})
    assert _drain(body) == b"HELLO"


def test_body_readinto_idempotent_at_eof():
    r = inst("_Reader")(_recv_of(b"ab"))
    body = inst("_make_body")(r, {b"content-length": b"2"})
    assert _drain(body) == b"ab"
    assert body.readinto(bytearray(4)) == 0    # re-reading past EOF stays at 0


def test_body_chunked():
    raw = b"5\r\nHELLO\r\n6\r\n world\r\n0\r\n\r\n"
    r = inst("_Reader")(_recv_of(raw))
    body = inst("_make_body")(r, {b"transfer-encoding": b"chunked"})
    assert _drain(body) == b"HELLO world"


def test_body_chunked_with_trailers():
    raw = b"3\r\nabc\r\n0\r\nX-Trailer: v\r\n\r\n"
    r = inst("_Reader")(_recv_of(raw))
    body = inst("_make_body")(r, {b"transfer-encoding": b"Chunked"})
    assert _drain(body) == b"abc"


def test_body_close_delimited():
    r = inst("_Reader")(_recv_of(b"all", b"the", b"bytes"))
    body = inst("_make_body")(r, {})
    assert _drain(body) == b"allthebytes"


def test_body_content_length_truncated_raises():
    r = inst("_Reader")(_recv_of(b"abc"))           # promises 10, gives 3
    body = inst("_make_body")(r, {b"content-length": b"10"})
    with pytest.raises(ValueError):
        _drain(body)


def test_body_chunked_truncated_raises():
    r = inst("_Reader")(_recv_of(b"5\r\nab"))        # chunk claims 5, only 2 arrive
    body = inst("_make_body")(r, {b"transfer-encoding": b"chunked"})
    with pytest.raises(ValueError):
        _drain(body)


def test_make_body_bad_content_length():
    with pytest.raises(ValueError):
        inst("_make_body")(inst("_Reader")(_recv_of(b"")), {b"content-length": b"x"})


# --- _install_stream --------------------------------------------------------

class _FakeFlash:
    """A FRONT slot as a bytearray, exposing the erase/write/readback closures the
    installer drives, so the whole write loop is testable without hardware."""

    def __init__(self, size):
        self.size = size
        self.mem = bytearray(b"\x00" * size)   # not yet erased
        self.writes = []

    def erase(self, total):
        self.mem[:total] = b"\xff" * total

    def write(self, off, data):
        self.mem[off:off + len(data)] = data
        self.writes.append((off, len(data)))

    def readback(self, off, n):
        return bytes(self.mem[off:off + n])


def _reader_of(data):
    """A read(n) callable yielding ``data`` in <=n slices then b''."""
    box = {"d": data}

    def read(n):
        out, box["d"] = box["d"][:n], box["d"][n:]
        return out
    return read


def _noop():
    pass


def _run_install(image, front_size, block, feed=_noop, progress=None, expect_sha=None,
                 repr_marker=None):
    flash = _FakeFlash(front_size)
    flash.erase(front_size)                 # the caller erases before _install_stream now
    inst("_install_stream")(_reader_of(image), flash.write,
                            flash.readback, front_size, block, feed, progress, expect_sha,
                            repr_marker)
    return flash


def test_install_stream_writes_and_arms():
    block = 4096
    front = 4 * block
    body = b"APP." + b"\x00" * 100
    image = bytearray(b"\xff" * front)
    image[:len(body)] = body
    trailer = b"OMVR-trailer"
    image[front - block:front - block + len(trailer)] = trailer

    flash = _run_install(bytes(image), front, block)
    assert flash.mem[:len(body)] == body
    assert flash.mem[front - block:front - block + len(trailer)] == trailer
    # PENDING armed last, in the status sector
    assert flash.mem[front - 2 * block:front - 2 * block + 16] == inst("PENDING")
    # the all-0xFF gap was never written (skipped)
    assert all(off < len(body) or off >= front - block for off, _ in flash.writes
               if off < front - 2 * block)


def test_install_stream_feeds_the_watchdog_per_chunk():
    block = 4096
    front = 3 * block
    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"
    calls = []
    _run_install(bytes(image), front, block, lambda: calls.append(1))
    # fed once per chunk through the erase-verify + write loops (not masking a hang)
    assert len(calls) >= front // block


def test_install_stream_reports_progress_per_chunk():
    block = 4096
    front = 3 * block
    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"
    seen = []
    _run_install(bytes(image), front, block, progress=lambda d, t: seen.append((d, t)))
    # one report per written chunk, advancing to a full slot, total always front_size
    assert seen[-1] == (front, front)
    assert all(t == front for _, t in seen)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)


class _RecordLog:
    def __init__(self):
        self.lines = []

    def info(self, msg, *a):
        self.lines.append(msg)


def test_progress_logs_to_the_injected_logger_at_ten_percent_steps():
    # The installer's _Progress is defined in installer.py so exec() puts it in RAM (safe
    # to call after the FRONT erase); it logs only, throttled to each new 10% step.
    rec = _RecordLog()
    p = inst("_Progress")(rec)
    for done in (4, 8, 40, 100):
        p(done, 100)
    assert rec.lines == [
        "install: 4% (4/100 bytes)",
        "install: 40% (40/100 bytes)",
        "install: 100% (100/100 bytes)",
    ]


def test_progress_zero_total_is_full():
    rec = _RecordLog()
    inst("_Progress")(rec)(0, 0)               # empty image -> 100%, no divide-by-zero
    assert rec.lines == ["install: 100% (0/0 bytes)"]


def test_install_stream_repr_marker_verify_fails():
    block, front = 4096, 2 * 4096
    so = front - 2 * block

    class DropRepr(_FakeFlash):
        def write(self, off, data):
            if off == so + 48:                            # pretend the repr write vanished
                return
            super().write(off, data)

    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"
    flash = DropRepr(front)
    flash.erase(front)
    with pytest.raises(OSError):
        inst("_install_stream")(_reader_of(bytes(image)), flash.write,
                                flash.readback, front, block, _noop, None, None,
                                inst("REPR_FULL"))


def test_install_stream_sha_ok():
    import hashlib
    block, front = 4096, 3 * 4096
    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"
    flash = _run_install(bytes(image), front, block,
                         expect_sha=hashlib.sha256(bytes(image)).hexdigest())
    assert flash.mem[:4] == b"DATA"                       # sha matched -> installed


def test_install_stream_sha_mismatch_raises():
    block, front = 4096, 2 * 4096
    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"
    with pytest.raises(OSError):
        _run_install(bytes(image), front, block, expect_sha="00" * 32)


# --- signed manifest: parse/select/reject/floor mirror the host codec --------

def _host_manifest(body=None, alg=None, key_id=0x0100):
    from openmv_ota.ota import ES256, algorithm_for
    from openmv_ota.ota.keys import generate_private_key
    from openmv_ota.ota.manifest import Manifest, pack_manifest, signed_region
    from openmv_ota.ota.sign import sign_region
    spec = algorithm_for(alg or ES256)
    priv = generate_private_key(spec)
    if body is None:
        body = {"schema": 1, "product_id": 7, "payload_version": 33685760,
                "min_platform_version": 0, "sha256": "ab" * 32,
                "representations": [{"format": "full", "url": "https://x/f.gz", "size": 9}]}
    m = Manifest(body=body, key_id=key_id, sig_alg=spec.cose_id)
    m.signature = sign_region(priv, signed_region(m), spec)
    return pack_manifest(m)


def test_manifest_parse_mirrors_host():
    from openmv_ota.ota.manifest import parse_manifest, signed_region
    raw = _host_manifest(key_id=0x0123)
    got = inst("_manifest_parse")(raw)
    host = parse_manifest(raw)
    assert got["body"] == host.body
    assert got["key_id"] == host.key_id == 0x0123
    assert got["sig_alg"] == host.sig_alg
    assert got["signature"] == host.signature
    assert got["region"] == signed_region(raw)            # the bytes the signature covers


def test_manifest_parse_rejections():
    import struct
    from openmv_ota.ota import ES384
    good = _host_manifest()
    with pytest.raises(ValueError, match="too small"):
        inst("_manifest_parse")(b"\x00" * 4)
    bad = bytearray(good)
    bad[0:4] = b"XXXX"
    with pytest.raises(ValueError, match="magic"):
        inst("_manifest_parse")(bytes(bad))
    bad = bytearray(good)
    struct.pack_into("<I", bad, 4, 2)                            # header_version
    with pytest.raises(ValueError, match="header_version"):
        inst("_manifest_parse")(bytes(bad))
    bad = bytearray(good)
    struct.pack_into("<i", bad, struct.calcsize("<4sIIII"), ES384)
    with pytest.raises(ValueError, match="alg/sig_size"):
        inst("_manifest_parse")(bytes(bad))
    with pytest.raises(ValueError, match="truncated"):
        inst("_manifest_parse")(good[:-1])
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    with pytest.raises(ValueError, match="crc"):
        inst("_manifest_parse")(bytes(bad))


def test_manifest_parse_unknown_alg():
    import struct
    bad = bytearray(_host_manifest())
    struct.pack_into("<i", bad, struct.calcsize("<4sIIII"), -99)
    with pytest.raises(ValueError, match="alg/sig_size"):
        inst("_manifest_parse")(bytes(bad))


@pytest.mark.parametrize(("body", "board", "plat", "floor"), [
    ({"schema": 2}, 7, 0, 0),
    ({"schema": 1, "product_id": 9}, 7, 0, 0),
    ({"schema": 1, "product_id": 7, "min_platform_version": 100}, 7, 50, 0),
    ({"schema": 1, "product_id": 7, "payload_version": 5}, 7, 0, 10),
    ({"schema": 1, "product_id": 7, "payload_version": 10}, 7, 0, 5),
    ({"schema": 1, "product_id": 9}, 0, 0, 0),               # device product_id 0 disables check
])
def test_update_reject_mirrors_host(body, board, plat, floor):
    from openmv_ota.ota.manifest import update_reject_reason
    assert (inst("_update_reject")(body, board, plat, floor)
            == update_reject_reason(body, board, plat, floor))


@pytest.mark.parametrize(("body_account", "dev_account", "expect"), [
    ("acctB", "acctA", "account"),      # mismatch -> reject, mirroring the host
    ("acctA", "acctA", None),           # match -> pass
    ("acctB", "", None),                # device has no account ('' = self-host) -> no check
])
def test_update_reject_account_mirrors_host(body_account, dev_account, expect):
    from openmv_ota.ota.manifest import update_reject_reason
    body = {"schema": 1, "product_id": 7, "payload_version": 10, "account_id": body_account}
    got = inst("_update_reject")(body, 7, 0, 0, dev_account)
    assert got == expect == update_reject_reason(body, 7, 0, 0, dev_account)


@pytest.mark.parametrize(("capable", "golden"), [(False, 0), (True, 100), (True, 999)])
def test_select_rep_mirrors_host(capable, golden):
    from openmv_ota.ota.manifest import select_representation
    body = {"representations": [
        {"format": "full", "url": "https://x/f.gz", "size": 900},
        {"format": "ocdl", "url": "https://x/d.gz", "size": 40, "base_payload_version": 100},
        {"format": "lzma", "url": "https://x/w.gz", "size": 1},
    ]}
    assert (inst("_select_rep")(body, capable, golden)
            == select_representation(body, capable, golden))


def test_select_rep_none_when_nothing_usable():
    body = {"representations": [
        {"format": "ocdl", "url": "https://x/d.gz", "size": 40, "base_payload_version": 1}]}
    assert inst("_select_rep")(body, False, 0) is None


def test_golden_floor_mirrors_trailer():
    import hashlib

    from openmv_ota.ota import ES256, Trailer, algorithm_for, pack_trailer, signed_region
    from openmv_ota.ota.keys import generate_private_key
    from openmv_ota.ota.sign import sign_region
    from openmv_ota.ota.version import encode_app_version
    pv = encode_app_version("3.4.5")
    spec = algorithm_for(ES256)
    priv = generate_private_key(spec)
    body = b"B" * 48
    t = Trailer(body_size=len(body), pad_size=0, meta={}, product_id=7, min_platform_version=0,
                payload_version=pv, payload_version_floor=0, key_id=0x0100, sig_alg=ES256,
                body_sha256=hashlib.sha256(body).digest())
    t.signature = sign_region(priv, signed_region(t), spec)
    trailer = pack_trailer(t)
    assert inst("_golden_floor")(trailer) == pv            # reads payload_version
    assert inst("_golden_floor")(b"\x00" * 4) == 0         # too short -> floor 0
    assert inst("_golden_floor")(b"XXXX" + trailer[4:]) == 0  # bad magic -> floor 0


# --- delta apply: device streaming mirror of ota.delta.apply_delta -----------

def _old_read_of(base):
    return lambda off, n: base[off:off + n]


class _SrcOf:
    """A src.read(n) over raw patch bytes (stands in for the DeflateIO patch stream)."""
    def __init__(self, data, step=7):
        self.data, self.pos, self.step = data, 0, step

    def read(self, n):
        n = min(n, self.step)                             # dribble it out to exercise buffering
        out = self.data[self.pos:self.pos + n]
        self.pos += len(out)
        return out


def test_delta_format_pinned_to_host():
    from openmv_ota.ota.manifest import DELTA_FORMAT
    assert inst("_DELTA_FORMAT") == DELTA_FORMAT


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_delta_stream_mirrors_host_apply(seed):
    from openmv_ota.ota.delta import apply_delta, make_delta
    base = bytes((i * 31 + seed) & 0xFF for i in range(8000))
    target = bytearray(base[:3000] + b"INSERTED-NEW-BYTES" + base[3200:] + b"tail" * 50)
    for i in range(0, len(target), 50):                   # scattered edits -> nonzero diffs
        target[i] ^= 0x5A
    target = bytes(target)
    patch = make_delta(base, target)
    gen = inst("_delta_stream")(inst("_PatchReader")(_SrcOf(patch)), _old_read_of(base), 256)
    assert b"".join(bytes(p) for p in gen) == apply_delta(base, patch) == target


def test_gen_reader_serves_read_n():
    from openmv_ota.ota.delta import make_delta
    base = bytes(range(256)) * 30
    target = base[:2000] + b"X" * 40 + base[2000:]
    patch = make_delta(base, target)
    gen = inst("_delta_stream")(inst("_PatchReader")(_SrcOf(patch)), _old_read_of(base), 512)
    rd = inst("_GenReader")(gen)
    out = b""
    while True:
        d = rd.read(100)
        if not d:
            break
        out += d
    assert out == target


def test_delta_stream_bad_magic():
    gen = inst("_delta_stream")(inst("_PatchReader")(_SrcOf(b"NOPE\x00\x00")),
                                _old_read_of(b""), 64)
    with pytest.raises(OSError, match="bad delta"):
        list(gen)


def test_patch_reader_truncated_varint_and_exact():
    pr = inst("_PatchReader")(_SrcOf(b""))
    with pytest.raises(OSError, match="truncated"):
        pr.read_uvarint()
    pr2 = inst("_PatchReader")(_SrcOf(b"ab"))
    with pytest.raises(OSError, match="truncated"):
        pr2.read_exact(8)


def test_add_zero_copy_and_pure_add():
    # all-zero diff -> straight copy; nonzero -> (old+diff) mod 256 (pure fallback on host)
    assert inst("_add")(b"\x10\x20", b"\x00\x00") == b"\x10\x20"
    assert inst("_add")(b"\xff\x02", b"\x01\x05") == b"\x00\x07"   # wraps mod 256


# --- _read_all (the manifest is read into RAM, not streamed) -----------------

class _FakeBody:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def readinto(self, buf):
        chunk = self.data[self.pos:self.pos + len(buf)]
        buf[:len(chunk)] = chunk
        self.pos += len(chunk)
        return len(chunk)


def test_read_all_reads_to_eof():
    assert inst("_read_all")(_FakeBody(b"manifest" * 200), 100000) == b"manifest" * 200


def test_read_all_rejects_oversize():
    with pytest.raises(ValueError, match="larger than"):
        inst("_read_all")(_FakeBody(b"x" * 5000), 1000)


def test_install_stream_image_too_large():
    block = 4096
    front = 2 * block
    with pytest.raises(ValueError):
        _run_install(b"\xff" * (front + block), front, block)


def test_install_stream_image_too_small():
    block = 4096
    front = 3 * block
    with pytest.raises(ValueError):
        _run_install(b"\xff" * (front - block), front, block)


def test_install_stream_erase_verify_fails():
    block = 4096
    front = 2 * block

    class BadErase(_FakeFlash):
        def erase(self, total):
            pass                                  # erase silently does nothing

    flash = BadErase(front)
    flash.erase(front)                      # the caller's erase silently did nothing
    with pytest.raises(OSError):            # _install_stream's read-back verify catches it
        inst("_install_stream")(_reader_of(b"\xff" * front), flash.write,
                                flash.readback, front, block, _noop)


def test_install_stream_write_verify_fails():
    block = 4096
    front = 2 * block

    class BadWrite(_FakeFlash):
        def write(self, off, data):
            self.mem[off:off + len(data)] = b"\x00" * len(data)  # corrupt the write

    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"
    flash = BadWrite(front)
    flash.erase(front)
    with pytest.raises(OSError):
        inst("_install_stream")(_reader_of(bytes(image)), flash.write,
                                flash.readback, front, block, _noop)


def test_install_stream_arm_verify_fails():
    block = 4096
    front = 2 * block

    class DropPending(_FakeFlash):
        def write(self, off, data):
            if data == inst("PENDING"):
                return                            # pretend the arm write vanished
            super().write(off, data)

    flash = DropPending(front)
    flash.erase(front)
    with pytest.raises(OSError):
        inst("_install_stream")(_reader_of(b"\xff" * front), flash.write,
                                flash.readback, front, block, _noop)
