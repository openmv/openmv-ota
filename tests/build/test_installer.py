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


def _run_install(image, front_size, block):
    flash = _FakeFlash(front_size)
    inst("_install_stream")(_reader_of(image), flash.erase, flash.write,
                            flash.readback, front_size, block)
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
    with pytest.raises(OSError):
        inst("_install_stream")(_reader_of(b"\xff" * front), flash.erase, flash.write,
                                flash.readback, front, block)


def test_install_stream_write_verify_fails():
    block = 4096
    front = 2 * block

    class BadWrite(_FakeFlash):
        def write(self, off, data):
            self.mem[off:off + len(data)] = b"\x00" * len(data)  # corrupt the write

    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"
    flash = BadWrite(front)
    with pytest.raises(OSError):
        inst("_install_stream")(_reader_of(bytes(image)), flash.erase, flash.write,
                                flash.readback, front, block)


def test_install_stream_arm_verify_fails():
    block = 4096
    front = 2 * block

    class DropPending(_FakeFlash):
        def write(self, off, data):
            if data == inst("PENDING"):
                return                            # pretend the arm write vanished
            super().write(off, data)

    flash = DropPending(front)
    with pytest.raises(OSError):
        inst("_install_stream")(_reader_of(b"\xff" * front), flash.erase, flash.write,
                                flash.readback, front, block)
