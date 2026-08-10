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


def test_ab_constants_match_boot_and_host():
    """The installer WRITES what boot.py READS. These three copies of the layout (host
    builder, installer, boot.py) exist because the device modules cannot import the host
    package, so drift is only caught here."""
    from openmv_ota.ota import rollback, status

    import tests.build.test_device_boot as boot_test        # noqa: PLC0415  (the frozen boot.py)
    B = boot_test.B
    assert (inst("_COUNTER_OFF"), inst("_COUNTER_LEN")) == (
        status.COUNTER_OFFSET, status.COUNTER_SIZE) == (B._COUNTER_OFF, B._COUNTER_LEN)
    assert inst("_ROLLBACK_ENTRY") == rollback.ENTRY_SIZE == B._ROLLBACK_ENTRY
    assert inst("_encode_counter")(9) == status.encode_counter(9)
    assert inst("_install_counter")(status.encode_counter(9).rjust(
        status.COUNTER_OFFSET + status.COUNTER_SIZE, b"\xff")) == 9
    assert inst("_install_counter")(b"\xff" * 80) is None    # blank -> unknown
    assert inst("_install_counter")(b"\xff" * 4) is None     # too short -> unknown
    torn = bytearray(b"\xff" * 80)
    torn[status.COUNTER_OFFSET:status.COUNTER_OFFSET + 4] = b"\x05\x00\x00\x00"
    assert inst("_install_counter")(bytes(torn)) is None     # value written, check not


def test_rollback_floor_of_mirrors_boot_and_host():
    """The installer reads the floor off both slots to carry the max forward; boot.py reads it
    to reject a downgrade. Same sector, three decoders -- pin them together."""
    from openmv_ota.ota import rollback

    import tests.build.test_device_boot as boot_test        # noqa: PLC0415
    sector = bytearray(b"\xff" * 4096)
    sector[0:8] = rollback.encode_entry(0x01000000)
    sector[8:16] = rollback.encode_entry(0x01020000)
    sector[16:20] = b"\x05\x00\x00\x00"                       # a torn entry: ignored, not trusted
    assert (inst("_rollback_floor_of")(bytes(sector))
            == boot_test.B._rollback_floor_of(bytes(sector))
            == rollback.floor_of(sector) == 0x01020000)
    assert inst("_rollback_floor_of")(b"\xff" * 4096) == 0        # blank -> no floor
    assert inst("_rollback_floor_of")(b"\xff" * 4) == 0           # shorter than one entry


def test_slot_table_mirrors_boot():
    import tests.build.test_device_boot as boot_test        # noqa: PLC0415
    B = boot_test.B

    for partition, front in ((8192, 0), (8192, 8192), (8192, 4096), (12288, 4096)):
        ob = B.OtaBoot(None, None, None, None, partition, front, 4096, 0, {}, 0)
        assert inst("_slot_table")(partition, front) == ob._slots()


@pytest.mark.parametrize("running,counters,expect", [
    # the normal case: never the running slot, whatever the counters say
    ("A", {"A": 5, "B": 4}, ("B", 6)),
    ("B", {"A": 4, "B": 5}, ("A", 6)),
    # ...INCLUDING when the running slot somehow looks older -- exclusion beats ordering,
    # because overwriting the image we are executing is the one unrecoverable move.
    ("A", {"A": 1, "B": 9}, ("B", 10)),
    # no running slot (recovery): fall back to the oldest, unreadable counting as oldest
    (None, {"A": 5, "B": 4}, ("B", 6)),
    (None, {"A": None, "B": 4}, ("A", 5)),
    (None, {"A": 4, "B": None}, ("B", 5)),
    (None, {"A": None, "B": None}, ("B", 1)),   # nothing to order by -> defined, not incidental
    (None, {"A": 4, "B": 4}, ("B", 5)),         # tie -> last listed
])
def test_install_target_never_picks_the_running_slot(running, counters, expect):
    slots = inst("_slot_table")(8192, 4096)
    name, off, size, counter = inst("_install_target")(slots, running, counters)
    assert (name, counter) == expect
    assert (off, size) == ((0, 4096) if name == "A" else (4096, 4096))


def test_install_target_in_single_mode_is_the_only_slot():
    """SINGLE mode's whole price: the target IS the running image. The counter still advances,
    so a device that later gains a second slot orders correctly."""
    slots = inst("_slot_table")(8192, 0)
    assert inst("_install_target")(slots, "A", {"A": 3}) == ("A", 0, 8192, 4)


def test_install_counter_outranks_an_unreadable_neighbour():
    """max(existing)+1, not other+1: the new image must sort above a slot whose counter we
    could not read, or a torn counter on the OTHER slot could outrank a good install."""
    slots = inst("_slot_table")(8192, 4096)
    assert inst("_install_target")(slots, "A", {"A": 7, "B": None})[3] == 8


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


# --- _clamp_to: the XIP tail guard ------------------------------------------
#
# This arithmetic is what keeps every memory-mapped alias clear of the last bytes of the
# partition. Getting it wrong in one direction re-opens a SILENT BRICK (a bulk read that
# reaches the end of an STM32H7 QUADSPI wedges the peripheral and the board just stops
# mid-install); in the other it stops verifying the tail of every slot. Hence unit tests
# rather than trusting a hardware run to notice.

@pytest.mark.parametrize("off,n,end,want", [
    (0, 4096, 8 << 20, 4096),                 # nowhere near the end: untouched
    ((8 << 20) - 4096 - 512, 4096, (8 << 20) - 512, 4096),   # ends exactly ON the guard: untouched
    ((8 << 20) - 4096, 4096, (8 << 20) - 512, 3584),         # the final block: shortened
    ((8 << 20) - 16, 16, (8 << 20) - 512, 0),                # wholly inside the guard: nothing
    ((8 << 20) - 512, 512, (8 << 20) - 512, 0),              # starts exactly at the guard
    (0, 0, 8 << 20, 0),
])
def test_clamp_to(off, n, end, want):
    assert inst("_clamp_to")(off, n, end) == want


def test_clamp_to_never_reaches_the_guarded_tail():
    """The property that actually matters: no (off, n) can produce a range crossing ``end``."""
    end = 4096 - 512
    for off in range(0, 4096, 7):
        n = inst("_clamp_to")(off, 4096 - off, end)
        assert n >= 0
        assert off + n <= end or n == 0


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


def test_reader_feeds_while_waiting_then_reads():
    # main-thread progress-fed recv: a non-blocking recv returns None until data arrives, feeding each
    # slice -- no select.poll(), no relax(); the feed cadence is driven from the loop itself
    fed = []
    seq = iter([None, None, None, b"payload"])
    r = inst("_Reader")(lambda _n: next(seq), feed=lambda: fed.append(1))
    assert r.read_exact(7) == b"payload"       # 3 no-data slices then the recv that returns data
    assert len(fed) == 4                        # fed each slice incl. the one that read data


def test_reader_would_block_none_then_data():
    # a non-blocking recv returns None while no data has arrived; the reader must keep feeding, not EOF
    seq = iter([None, None, b"hi"])
    fed = []
    r = inst("_Reader")(lambda _n: next(seq), feed=lambda: fed.append(1))
    assert r.read_exact(2) == b"hi"
    assert len(fed) == 3                        # fed on each no-data slice before data arrived


def test_reader_eof_returns_false():
    # a non-blocking recv of b'' is a real EOF -> _fill returns False
    r = inst("_Reader")(_recv_of(), feed=lambda: None)
    assert r.read_some(5) == b""


def test_reader_recv_eagain_then_data():
    # some ports RAISE OSError(EAGAIN) instead of returning None; treat as would-block, keep going
    state = {"n": 0}

    def recv(_n):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError(11)                  # EAGAIN
        return b"ok"
    r = inst("_Reader")(recv, feed=lambda: None)
    assert r.read_exact(2) == b"ok"


def test_reader_recv_oserror_propagates():
    # a non-would-block OSError (e.g. ECONNRESET) is a real failure -> propagate (install -> golden)
    def recv(_n):
        raise OSError(104)                     # ECONNRESET
    r = inst("_Reader")(recv, feed=lambda: None)
    with pytest.raises(OSError):
        r.read_exact(1)


def test_reader_dead_link_trips_after_timeout():
    # a link that never produces data feeds until _SOCK_TIMEOUT, then raises (-> clean install -> golden)
    r = inst("_Reader")(lambda _n: None, feed=lambda: None)
    with pytest.raises(OSError, match="timed out"):
        r.read_exact(1)


# --- _ResumingBody: surviving a link that drops mid-download -------------------------------
# A poor link cannot finish a long download in one connection (the WINC1500 aborts EVERY transfer
# at ~50 s regardless of progress). Restarting is wrong -- the decoder's state lives in RAM and
# survives the dead socket -- so only the transport is replaced, via Range at the consumed offset.
# These pin that the byte stream handed to the decoder stays CONTINUOUS across a reconnect, which
# is the property that makes it safe: a duplicated or dropped byte would corrupt the image.

class _DropSock:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _DropBody:
    """Serves ``data`` from ``start``, raising OSError after ``fail_after`` bytes (None = never)."""

    def __init__(self, data, start=0, fail_after=None):
        self._data, self._pos = data, start
        self._left = fail_after

    def readinto(self, buf):
        if self._left is not None and self._left <= 0:
            raise OSError(103)                       # ECONNABORTED, mid-stream
        n = min(len(buf), len(self._data) - self._pos)
        if self._left is not None:
            n = min(n, self._left)
            self._left -= n
        buf[:n] = self._data[self._pos:self._pos + n]
        self._pos += n
        return n


def _drain(body, chunk=7):
    out, buf = bytearray(), bytearray(chunk)
    while True:
        n = body.readinto(buf)
        if not n:
            return bytes(out)
        out += buf[:n]


def _resuming(monkeypatch, data, drops):
    """A _ResumingBody over ``data`` whose connections fail after each of ``drops`` bytes.
    Records the Range offsets requested so the test can assert continuity."""
    calls = []
    seq = list(drops)

    def fake_open(url, ca, socket, ssl, feed=None, max_redirects=5, start=0):
        calls.append(start)
        fail = seq.pop(0) if seq else None
        return _DropSock(), _DropBody(data, start, fail)

    monkeypatch.setattr(_mod, "_open", fake_open)
    sock, body = fake_open("u", None, None, None)
    return inst("_ResumingBody")("u", None, None, None, lambda: None, sock, body), calls


def test_resuming_body_is_a_micropython_stream():
    # REGRESSION GUARD. deflate.DeflateIO reads its source through MicroPython's C-level stream
    # protocol, and a Python object only participates in that by subclassing io.IOBase -- having a
    # readinto() method is NOT enough. Shipping this as a plain class made every install die at the
    # first read with OSError('stream operation not supported'), which on hardware looked like a
    # mid-install failure falling back to golden. CPython reads any object with the right methods,
    # so no behavioural host test can reproduce it; asserting the base class is the only host-side
    # guard, and _Body (the stream this replaces) has always subclassed it for exactly this reason.
    import io
    assert issubclass(inst("_ResumingBody"), io.IOBase)
    assert issubclass(inst("_Body"), io.IOBase)


def test_resuming_body_passes_through_when_nothing_drops(monkeypatch):
    data = bytes(range(256)) * 4
    body, calls = _resuming(monkeypatch, data, [None])
    assert _drain(body) == data
    assert calls == [0]                              # never reconnected


def test_resuming_body_reconnects_at_the_consumed_offset(monkeypatch):
    # the whole point: the decoder must see EXACTLY the original bytes, once each, in order
    data = bytes(range(256)) * 8                     # 2048 bytes
    body, calls = _resuming(monkeypatch, data, [500, 700, None])
    assert _drain(body) == data                      # byte-identical -> no gap, no duplication
    assert calls == [0, 500, 1200]                   # resumed exactly where it left off


def test_resuming_body_is_unbounded_while_progressing(monkeypatch):
    # many drops, but each delivers bytes -> must keep going, NOT hit a retry cap. An attempt cap
    # would abandon a slow-but-working link for being slow rather than for being broken.
    data = bytes(range(256)) * 8
    drops = [64] * 40                                # 40 reconnects, far above _RESUME_MAX_STALLS
    body, _ = _resuming(monkeypatch, data, drops + [None])
    assert _drain(body) == data


def test_resuming_body_gives_up_when_truly_stuck(monkeypatch):
    # zero-byte reconnects are the honest "stuck" signal -> bounded, so it falls to golden
    data = bytes(range(256))
    body, calls = _resuming(monkeypatch, data, [0] * 50)
    with pytest.raises(OSError):
        _drain(body)
    assert len(calls) == inst("_RESUME_MAX_STALLS") + 1   # the initial open + the budgeted re-opens


def test_resuming_body_progress_resets_the_stall_budget(monkeypatch):
    # near-stuck then progress must NOT accumulate toward the cap, or a link that stutters and
    # then recovers would be abandoned partway through a perfectly good download
    data = bytes(range(256)) * 4
    drops = ([0] * 9 + [200]) * 3                    # 9 stalls, progress, repeat -- never 10 in a row
    body, _ = _resuming(monkeypatch, data, drops + [None])
    assert _drain(body) == data


def test_resuming_body_tolerates_a_socket_that_cannot_be_closed(monkeypatch):
    # The socket we are replacing is usually ALREADY dead -- that is why we are reconnecting -- so
    # close() can itself raise (EBADF / a torn-down TLS wrapper). That must not abort an otherwise
    # healthy resume, nor close(): there is nothing left to release either way. Without the guards
    # a poor link would turn every reconnect into a failed install.
    data = bytes(range(256)) * 4
    calls = []
    seq = [200, None]

    class _UncloseableSock:
        def close(self):
            raise OSError(9)                         # EBADF -- the peer is already gone

    def fake_open(url, ca, socket, ssl, feed=None, max_redirects=5, start=0):
        calls.append(start)
        return _UncloseableSock(), _DropBody(data, start, seq.pop(0) if seq else None)

    monkeypatch.setattr(_mod, "_open", fake_open)
    sock, body = fake_open("u", None, None, None)
    rb = inst("_ResumingBody")("u", None, None, None, lambda: None, sock, body)
    assert _drain(rb) == data                        # resumed despite close() raising
    assert calls == [0, 200]
    rb.close()                                       # and close() swallows it too


def test_resuming_body_close_closes_the_current_socket(monkeypatch):
    # after a resume the body owns a NEWER socket; closing the original would leak the live one
    data = bytes(range(256)) * 4
    body, _ = _resuming(monkeypatch, data, [100, None])
    body.readinto(bytearray(300))                    # forces one reconnect
    body.readinto(bytearray(300))
    body.close()
    assert body._sock.closed


def test_is_transport_error_keys_on_phase_not_exception_type():
    # run()'s pre-erase except uses this to tell a CONNECTION failure (defer + retry, marker-less)
    # from a REJECTED update (install.reject). The classifier keys on the PHASE -- _fetch_manifest
    # wraps its open+read in _TransportError -- because the exception type cannot separate them:
    # a socket drop and a TLS/cert failure are both OSError-with-a-number, and a corrupt manifest
    # and a clock-skew cert failure are both ValueError. Pin that so nobody "simplifies" it back
    # into type/errno sniffing.
    ite, terr = inst("_is_transport_error"), inst("_TransportError")
    assert ite(terr("manifest fetch failed: %r" % OSError(103)))     # ECONNABORTED (the WINC's flaky TLS)
    assert ite(terr("manifest fetch failed: %r" % OSError(-15202, "MBEDTLS_ERR_PK_INVALID_PUBKEY")))
    assert ite(terr("manifest fetch failed: %r"                      # cold clock, pre-NTP: recovers
                    % ValueError("certificate validity starts in the future")))  # a poll later
    # rejections -- raised AFTER the fetch, so never wrapped -- must stay on the reject side even
    # though two of them are OSError, exactly like the transport cases above
    assert not ite(OSError("manifest signature does not verify"))    # bad sig
    assert not ite(OSError("manifest signed by an untrusted key"))   # untrusted key
    assert not ite(OSError("manifest rejected (rollback)"))          # anti-rollback vetting
    assert not ite(ValueError("bad manifest magic"))                 # corrupt manifest
    assert not ite(OSError(103))                    # a RAW socket errno is not itself the wrapper
    assert issubclass(terr, OSError)                # so an app catching OSError still catches it


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


class _SourceOf:
    """A readinto(mv)->int source over fixed bytes, dribbling <=step per call to exercise the
    write loop's re-chunking fill. Stands in for dio / _GenReader in _install_stream tests."""

    def __init__(self, data, step=1500):
        self.data, self.pos, self.step = data, 0, step

    def readinto(self, mv):
        n = min(len(mv), self.step, len(self.data) - self.pos)
        mv[:n] = self.data[self.pos:self.pos + n]
        self.pos += n
        return n


def _noop():
    pass


def _run_install(image, front_size, block, feed=_noop, progress=None, expect_sha=None,
                 repr_marker=None, counter=None, floor=0):
    flash = _FakeFlash(front_size)
    flash.erase(front_size)                 # the caller erases before _install_stream now
    inst("_install_stream")(_SourceOf(image), flash.write,
                            flash.readback, front_size, block, feed, progress, expect_sha,
                            repr_marker, None, None, counter, floor)
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


def test_install_stream_accepts_a_readback_shortened_by_the_xip_tail_guard():
    """On an XIP port the last readback of the last slot comes back SHORT -- the guard keeps the
    alias off the final bytes of the partition. The write still has to verify: comparing the full
    chunk against a short read would fail every install on the H7 Plus, which is the board this
    guard exists for."""
    block, front, guard = 4096, 4 * 4096, 512
    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"
    image[front - block:front - block + 8] = b"TRAILER."
    image[front - 100:] = b"\x5a" * 100          # real data inside the guarded tail

    flash = _FakeFlash(front)
    flash.erase(front)
    readable = front - guard
    reads = []

    def readback(off, n):
        k = inst("_clamp_to")(off, n, readable)
        reads.append((off, n, k))
        return bytes(flash.mem[off:off + k])

    inst("_install_stream")(_SourceOf(bytes(image)), flash.write, readback,
                            front, block, _noop, None, None, None, None, None, None, 0)

    # it did not raise -> the shortened verify was accepted...
    assert any(k < n for _, n, k in reads), "the guard never actually shortened a read"
    # ...and the guarded bytes were still WRITTEN, they are merely not read back
    assert bytes(flash.mem[front - 100:]) == b"\x5a" * 100


def test_install_stream_still_catches_a_bad_write_outside_the_guard():
    """The shortened compare must not become a blanket 'any readback passes'."""
    block, front, guard = 4096, 4 * 4096, 512
    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"

    flash = _FakeFlash(front)
    flash.erase(front)
    readable = front - guard

    def readback(off, n):
        k = inst("_clamp_to")(off, n, readable)
        buf = bytearray(flash.mem[off:off + k])
        if off == 0 and buf:
            buf[0] ^= 0xFF                        # corrupt a byte well clear of the guard
        return bytes(buf)

    with pytest.raises(OSError):
        inst("_install_stream")(_SourceOf(bytes(image)), flash.write, readback,
                                front, block, _noop, None, None, None, None, None, None, 0)


def test_install_stream_stamps_the_counter_and_carries_the_floor():
    """The arm sequence, in order. Everything boot.py reads about this slot is written here,
    and PENDING -- the only thing that makes the slot bootable -- must be written last."""
    from openmv_ota.ota import rollback, status

    block, front = 4096, 5 * 4096
    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"
    flash = _run_install(bytes(image), front, block, counter=12, floor=0x01020000,
                         repr_marker=inst("REPR_FULL"))
    so, ro = front - 2 * block, front - 3 * block
    assert flash.mem[so + status.COUNTER_OFFSET:
                     so + status.COUNTER_OFFSET + status.COUNTER_SIZE] == \
        status.encode_counter(12)
    assert rollback.floor_of(flash.mem[ro:ro + block]) == 0x01020000
    # PENDING is the LAST write of the whole install -- nothing is bootable before it.
    assert flash.writes[-1][0] == so


def test_install_stream_without_a_floor_leaves_the_rollback_sector_blank():
    """floor 0 means nothing has ever been confirmed, so there is nothing to carry. Writing a
    zero entry would claim a floor of 0 rather than none -- harmless today, but it would burn
    an append slot per install for no information."""
    block, front = 4096, 5 * 4096
    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"
    flash = _run_install(bytes(image), front, block, counter=1, floor=0)
    ro = front - 3 * block
    assert flash.mem[ro:ro + block] == b"\xff" * block


def test_install_stream_counter_and_floor_verify_their_writes():
    """A flash that silently drops a write must fail the install, not produce a slot that
    boot.py cannot order (or one whose floor quietly regressed)."""
    block, front = 4096, 5 * 4096
    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"

    for drop in (front - 3 * block, front - 2 * block + 64):
        flash = _FakeFlash(front)
        flash.erase(front)
        real = flash.write

        def write(off, data, drop=drop, real=real):
            if off != drop:
                real(off, data)

        with pytest.raises(OSError):
            inst("_install_stream")(_SourceOf(bytes(image)), write, flash.readback,
                                    front, block, _noop, None, None, None, None, None,
                                    5, 0x01020000)


def test_install_stream_feeds_the_watchdog_per_chunk():
    block = 4096
    front = 3 * block
    image = bytearray(b"\xff" * front)
    image[:4] = b"DATA"
    calls = []
    _run_install(bytes(image), front, block, lambda: calls.append(1))
    # fed once per chunk through the erase-verify + write loops (not masking a hang)
    assert len(calls) >= front // block


def test_install_stream_gc_collect_runs_on_cadence():
    # With a watchdog armed the caller passes a proactive-collect hook; _install_stream calls it
    # on the _GC_EVERY byte cadence so automatic GC never fires mid-write. Never disables GC.
    block = 4096
    every = inst("_GC_EVERY")
    front = 3 * every                                # a few cadence intervals
    image = bytearray(b"\xff" * front)
    for i in range(0, front, block):                 # non-blank every chunk so each is written
        image[i:i + 4] = b"DATA"
    calls = []
    flash = _FakeFlash(front)
    flash.erase(front)
    inst("_install_stream")(_SourceOf(bytes(image), step=block), flash.write, flash.readback,
                            front, block, _noop, None, None, None, lambda: calls.append(1))
    assert len(calls) >= 2                            # fired on the byte cadence, not just once


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


def test_progress_reset_restarts_the_ten_percent_steps():
    # A retried download re-streams from 0; reset() rewinds the step counter so the
    # fresh attempt logs its 10% marks again instead of staying silent past the old high.
    rec = _RecordLog()
    p = inst("_Progress")(rec)
    p(50, 100)
    p.reset()
    p(10, 100)                                 # would be suppressed (< 50%) without reset
    assert rec.lines == [
        "install: 50% (50/100 bytes)",
        "install: 10% (10/100 bytes)",
    ]


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
        inst("_install_stream")(_SourceOf(bytes(image)), flash.write,
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


def test_trailer_version_mirrors_trailer():
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
    assert inst("_trailer_version")(trailer) == pv            # reads payload_version
    assert inst("_trailer_version")(b"\x00" * 4) == 0         # too short -> 0
    assert inst("_trailer_version")(b"XXXX" + trailer[4:]) == 0  # bad magic -> 0


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


def test_delta_stream_via_readinto_source():
    # drives _PatchReader's readinto() branch (the on-device DeflateIO path) rather than read()
    from openmv_ota.ota.delta import apply_delta, make_delta
    base = bytes((i * 17) & 0xFF for i in range(4000))
    target = base[:1000] + b"NEW-BYTES" + base[1009:]
    patch = make_delta(base, target)
    gen = inst("_delta_stream")(inst("_PatchReader")(_SourceOf(patch, step=100)),
                                _old_read_of(base), 256)
    assert b"".join(bytes(p) for p in gen) == apply_delta(base, patch) == target


def test_gen_reader_serves_readinto():
    # _GenReader re-chunks arbitrary delta pieces into the caller's fixed buffer via readinto,
    # carrying any partial piece across calls (buffer size 100 << the 512-byte delta chunk).
    from openmv_ota.ota.delta import make_delta
    base = bytes(range(256)) * 30
    target = base[:2000] + b"X" * 40 + base[2000:]
    patch = make_delta(base, target)
    gen = inst("_delta_stream")(inst("_PatchReader")(_SrcOf(patch)), _old_read_of(base), 512)
    rd = inst("_GenReader")(gen)
    out = bytearray()
    buf = bytearray(100)
    mv = memoryview(buf)
    while True:
        n = rd.readinto(mv)
        if n == 0:
            break
        out += mv[:n]
    assert bytes(out) == target


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
        inst("_install_stream")(_SourceOf(b"\xff" * front), flash.write,
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
        inst("_install_stream")(_SourceOf(bytes(image)), flash.write,
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
        inst("_install_stream")(_SourceOf(b"\xff" * front), flash.write,
                                flash.readback, front, block, _noop)


def test_log_is_a_null_logger_on_host():
    # openmv_log is absent off-device, so the installer's `log` is a null logger -- the
    # device paths call log.debug/info/warning/error unconditionally (no is-not-None guard).
    log = inst("log")
    assert log.debug("d") is None
    assert log.info("i") is None
    assert log.warning("w") is None
    assert log.error("e") is None
    assert log.critical("c") is None


def test_the_host_tick_fallbacks_are_usable():
    """CPython's `time` has no ticks_ms/ticks_diff, so the installer defines no-op stand-ins. They
    exist so the erase loop's "worst single flash op" timing compiles and runs on the host; on
    device the real monotonic ones are imported. A stand-in that raised would only surface on
    hardware, mid-install, which is the worst possible place to find out."""
    assert _mod.ticks_ms() == 0
    assert _mod.ticks_diff(7, 2) == 5
