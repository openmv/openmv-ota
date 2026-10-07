"""Host tests for ``openmv_cloud._lib`` -- the plumbing every wrapper shares.

The pure helpers that used to live in csi (URL parsing) and logs (session id,
record batching) and were reached into by sibling modules. The socket and
filesystem entry points (_open, _post_ndjson, _FileDisk, _open_disk,
_drain_disk) are device glue, exercised on hardware.
"""

from __future__ import annotations

import json

import pytest

from openmv_ota.build.device.openmv_cloud import _lib


# --- URL parsing ------------------------------------------------------------

@pytest.mark.parametrize(("url", "want"), [
    ("wss://live.cloud.openmv.io/camera/d1/0?token=t",
     (True, "live.cloud.openmv.io", 443, "/camera/d1/0?token=t")),
    ("https://live.cloud.openmv.io/poll/d1/tele?token=t",
     (True, "live.cloud.openmv.io", 443, "/poll/d1/tele?token=t")),
    ("ws://localhost:8787/camera/d1/0?token=t",
     (False, "localhost", 8787, "/camera/d1/0?token=t")),
    ("http://relay:8080/poll/d1/0", (False, "relay", 8080, "/poll/d1/0")),
])
def test_split_url(url, want):
    assert _lib._split_url(url) == want


@pytest.mark.parametrize("bad", ["ftp://x/y", "no-scheme", "https://"])
def test_split_url_rejects(bad):
    with pytest.raises(ValueError):
        _lib._split_url(bad)


# --- the backscroll contract: session id ------------------------------------

def test_session_id_is_hex_of_the_random_bytes():
    assert _lib._session_id(bytes(range(8))) == "0001020304050607"
    assert len(_lib._session_id()) == 16


# --- record batching --------------------------------------------------------

def _rec(sid, seq):
    return json.dumps({"sid": sid, "seq": seq, "text": "x\n"}).encode()


def test_batch_end_packs_records_within_max_bytes():
    recs = [b"12345", b"6789", b"abc"]                # +1 newline each when joined
    assert _lib._batch_end(recs, 0, 100) == 3         # all fit
    assert _lib._batch_end(recs, 0, 6) == 1           # only the first (5+1)
    assert _lib._batch_end(recs, 0, 1) == 1           # oversize still takes one
    assert _lib._batch_end(recs, 1, 6) == 2           # from the middle


def test_batch_end_never_crosses_a_sid_boundary():
    # a spool spanning a reboot: two sid runs, seq resets. A byte budget that
    # would swallow both runs must still stop at the boundary (one sid per batch).
    recs = [_rec("aa00", 0), _rec("aa00", 1), _rec("bb11", 0), _rec("bb11", 1)]
    assert _lib._batch_end(recs, 0, 10_000) == 2      # stops at the reboot boundary
    assert _lib._batch_end(recs, 2, 10_000) == 4      # the second run drains next


def test_rec_sid_of_a_non_record_line_is_none():
    assert _lib._rec_sid(b"12345") is None            # bare int
    assert _lib._rec_sid(b"not json") is None
    assert _lib._rec_sid(_rec("aa00", 0)) == "aa00"


# --- _batch_window: the streaming drain's decision function -----------------

def _win(*recs):
    return b"".join(r + b"\n" for r in recs)


def test_batch_window_takes_whole_records_only():
    win = _win(_rec("aa00", 0), _rec("aa00", 1))
    assert _lib._batch_window(win, 10_000) == len(win)      # both, exactly


def test_batch_window_ignores_a_trailing_partial_record():
    # a crash mid-append leaves a record with no newline: it must not be sent
    win = _win(_rec("aa00", 0)) + _rec("aa00", 1)[:10]
    assert _lib._batch_window(win, 10_000) == len(_win(_rec("aa00", 0)))


def test_batch_window_stops_at_a_sid_boundary():
    first = _win(_rec("aa00", 0))
    win = first + _win(_rec("bb11", 0))
    assert _lib._batch_window(win, 10_000) == len(first)


def test_batch_window_respects_the_byte_budget():
    one = _win(_rec("aa00", 0))
    win = one + _win(_rec("aa00", 1))
    assert _lib._batch_window(win, len(one)) == len(one)     # second won't fit


def test_batch_window_always_takes_at_least_one_record():
    # an oversize record must not wedge the drain forever
    win = _win(_rec("aa00", 0))
    assert _lib._batch_window(win, 1) == len(win)


def test_batch_window_returns_zero_when_no_record_is_complete():
    assert _lib._batch_window(_rec("aa00", 0)[:12], 10_000) == 0
    assert _lib._batch_window(b"", 10_000) == 0


# --- the shared RAM budget --------------------------------------------------

class _Sink:
    """A budget member: holds bytes, sheds its oldest on request."""
    def __init__(self, budget):
        self.items, self._budget = [], budget
        budget.join(self)

    def add(self, n):
        self.items.append(n)
        self._budget.charge(n)

    def pending_bytes(self):
        return sum(self.items)

    def shed(self):
        if self.items:
            self._budget.release(self.items.pop(0))


def test_budget_sheds_from_the_largest_member_first():
    # THE point of a shared pool: a chatty sink must not starve quiet ones.
    b = _lib._Budget(100)
    chatty, quiet = _Sink(b), _Sink(b)
    quiet.add(10)
    chatty.add(50)
    chatty.add(50)                                # total 110 -> over cap
    # the big sink shed; the quiet sink's 10 bytes were never touched
    assert quiet.items == [10]
    assert b.total() <= 100


def test_budget_leaves_small_members_alone_under_sustained_pressure():
    b = _lib._Budget(60)
    quiet = _Sink(b)
    quiet.add(5)
    chatty = _Sink(b)
    for _ in range(10):
        chatty.add(20)
    assert quiet.items == [5]                     # never evicted
    assert b.total() <= 60


def test_budget_stops_when_nothing_can_be_shed():
    b = _lib._Budget(10)
    b.charge(50)                                  # charged with no members
    b.enforce()                                   # must return, not spin
    assert b.total() == 50


def test_budget_release_never_goes_negative():
    b = _lib._Budget(10)
    b.charge(5)
    b.release(50)
    assert b.total() == 0


def test_budget_join_is_idempotent_and_leave_works():
    b = _lib._Budget(100)
    s = _Sink(b)
    b.join(s)                                     # already a member
    assert b._members == [s]
    b.leave(s)
    b.leave(s)                                    # tolerated twice
    assert b._members == []


# --- configure(): the app owns these knobs ----------------------------------

def test_configure_sets_limits_and_resizes_the_budget():
    old_budget, old_batch = _lib.limits.budget_bytes, _lib.limits.batch_bytes
    try:
        _lib.configure(budget_bytes=4096, batch_bytes=1024)
        assert _lib.limits.budget_bytes == 4096
        assert _lib.limits.batch_bytes == 1024
        assert _lib.budget.cap == 4096            # the live pool follows
    finally:
        _lib.configure(budget_bytes=old_budget, batch_bytes=old_batch)


def test_configure_rejects_a_typo_rather_than_ignoring_it():
    # silently ignoring would leave the app believing it had set a budget
    with pytest.raises(ValueError, match="unknown limit"):
        _lib.configure(budgetbytes=4096)
    with pytest.raises(ValueError, match="unknown limit"):
        _lib.configure(_members=1)


@pytest.mark.parametrize("bad", [0, -1, "4096", 4096.0])
def test_configure_rejects_a_nonsense_value(bad):
    with pytest.raises(ValueError, match="positive int"):
        _lib.configure(budget_bytes=bad)


def test_budget_stops_if_a_member_sheds_nothing():
    # a member that reports bytes but can't free them must not spin the loop
    class _Stuck:
        def pending_bytes(self):
            return 999
        def shed(self):
            pass                                  # frees nothing

    b = _lib._Budget(10)
    b.join(_Stuck())
    b.charge(50)                                  # triggers enforce -> one attempt
    assert b.total() == 50


# --- the record timestamp seam ----------------------------------------------

def test_timestamp_comes_from_openmv_rtc_when_present(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "openmv_rtc",
                        type("m", (), {"timestamp": staticmethod(lambda: 1234.5)}))
    monkeypatch.setattr(_lib, "_rtc", None)          # re-look
    assert _lib._timestamp() == 1234.5


def test_timestamp_is_none_when_the_clock_says_untrusted(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "openmv_rtc",
                        type("m", (), {"timestamp": staticmethod(lambda: None)}))
    monkeypatch.setattr(_lib, "_rtc", None)
    assert _lib._timestamp() is None


def test_timestamp_is_none_without_the_clock_module(monkeypatch):
    # a non-OTA firmware has no openmv_rtc; records simply carry (sid, seq)
    monkeypatch.setattr(_lib, "_rtc", False)
    assert _lib._timestamp() is None


def test_hard_close_closes_the_socket_not_the_noop_stream():
    from openmv_ota.build.device.openmv_cloud import _lib as lib

    class S:
        closed = 0

        def close(self):
            S.closed += 1

    class W:                            # asyncio Stream: close() does nothing, .s is the socket
        s = S()

        def close(self):
            raise AssertionError("not this one")
    lib._hard_close(W())
    assert S.closed == 1
    lib._hard_close(S())                # a plain socket closes itself
    assert S.closed == 2

    class Bad:
        def close(self):
            raise OSError(9)
    lib._hard_close(Bad())              # never raises


def test_relieve_conns_drops_every_open_datalake_connection(monkeypatch):
    from openmv_ota.build.device.openmv_cloud import _lib as lib

    class S:
        closed = False

        def close(self):
            self.closed = True

    class C:
        def __init__(self, open_):
            self._writer = S() if open_ else None
            self._reader = object() if open_ else None
    a, b = C(True), C(False)
    monkeypatch.setattr(lib, "_conns", [a, b])
    sa = a._writer
    assert lib._relieve_conns(0) == 1
    assert sa.closed and a._writer is None and a._reader is None and lib._conns == []


# --- TLS: the SDK opens its connections under the OTA runtime's one rule --------

class _Ssl:
    PROTOCOL_TLS_CLIENT, CERT_NONE, CERT_REQUIRED = 1, 0, 2

    class SSLContext:
        def __init__(self, proto):
            self.verify_mode, self.cadata = None, None

        def load_verify_locations(self, cadata):
            self.cadata = cadata


def _runtime(monkeypatch, builtin, flag):
    """The device openmv_ota runtime installed beside us, with no romfs ca.pem override,
    ``builtin`` frozen anchors, and _ota_config.TLS_VERIFY = ``flag``."""
    import sys
    import types

    from openmv_ota.build.device import openmv_ota as rt

    def no_override(*a):
        raise OSError("no data/ca.pem")
    monkeypatch.setattr(rt, "_read_file", no_override)
    monkeypatch.setattr(rt, "_tls_warned", False)
    monkeypatch.setitem(sys.modules, "openmv_ota", rt)
    monkeypatch.setitem(sys.modules, "_ota_config",
                        types.SimpleNamespace(CA_PEM=builtin, TLS_VERIFY=flag))
    monkeypatch.setattr(_lib, "_ca_pem", None)


def test_sdk_verifies_against_the_frozen_store(monkeypatch):
    _runtime(monkeypatch, b"PEM", True)
    assert _lib._ca() == b"PEM"
    ctx = _lib._tls_ctx(_Ssl)
    assert ctx.verify_mode == _Ssl.CERT_REQUIRED and ctx.cadata == "PEM"


def test_sdk_goes_unverified_only_on_a_build_that_allows_it(monkeypatch):
    _runtime(monkeypatch, b"", False)
    assert _lib._ca() is None
    assert _lib._tls_ctx(_Ssl).verify_mode == _Ssl.CERT_NONE


def test_sdk_refuses_without_anchors_on_a_verifying_build(monkeypatch):
    """It used to fall to MicroPython's default here -- CERT_NONE, silently. Now it refuses."""
    _runtime(monkeypatch, b"", True)
    with pytest.raises(OSError, match="no TLS trust anchors"):
        _lib._tls_ctx(_Ssl)


def test_sdk_refuses_without_the_ota_runtime(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "openmv_ota", None)
    monkeypatch.setattr(_lib, "_ca_pem", None)
    assert _lib._ca() is None
    with pytest.raises(ImportError):
        _lib._tls_ctx(_Ssl)


# --- the datalake connection's request/response codec (allocation-free on device) ----------

def test_put_and_put_int_write_in_place():
    buf = bytearray(16)
    off = _lib._put(buf, 0, b"len=")
    off = _lib._put_int(buf, off, 4096)
    assert bytes(buf[:off]) == b"len=4096"
    for n in (0, 7, 10, 65535, 1234567):
        b = bytearray(12)
        end = _lib._put_int(b, 2, n)
        assert bytes(b[2:end]) == str(n).encode()


def test_num_at_decimal_and_hex():
    buf = bytearray(b"  1234\r\n1aF;ext\r\nzz")
    assert _lib._num_at(buf, 0, 8, 10) == 1234           # leading spaces skipped
    assert _lib._num_at(buf, 8, 17, 16) == 0x1AF         # stops at ';'
    assert _lib._num_at(buf, 17, 19, 16) == -1           # no digits
    assert _lib._num_at(buf, 8, 17, 10) == 1             # a decimal read stops at the letter


def test_request_head_and_body_fill():
    buf = bytearray(256)
    n = _lib._request_head(buf, b"POST /ingest/a/p/d/", b"heap",
                           b" HTTP/1.1\r\nHost: h\r\nContent-Length: ", 42)
    assert bytes(buf[:n]) == b"POST /ingest/a/p/d/heap HTTP/1.1\r\nHost: h\r\nContent-Length: 42\r\n\r\n"
    recs = [b'{"a":1}', b'{"b":2}', b"x"]
    assert _lib._body_len(recs, b"\n") == len(b"\n".join(recs))
    assert _lib._body_len([], b"\n") == 0
    out = bytearray(64)
    assert bytes(out[:_lib._fill(out, recs, b"\n")]) == b"\n".join(recs)


_CF_HEAD = (b"HTTP/1.1 200 OK\r\nDate: x\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\nConnection: keep-alive\r\nServer: cloudflare\r\n\r\n")


def _resp(raw):
    buf = bytearray(len(raw) + 8)
    buf[:len(raw)] = raw
    return _lib._response(buf, len(raw))


def test_response_chunked_reply_from_cloudflare_is_read_and_reused():
    body = b"17\r\n" + b'{"stored":1,"bytes":83}' + b"\r\n0\r\n\r\n"
    assert _resp(_CF_HEAD + body) == (True, True)
    assert _resp(_CF_HEAD + body[:10]) is None            # mid-chunk: keep reading
    assert _resp(_CF_HEAD + b"1") is None                 # the size line not finished yet
    assert _resp(_CF_HEAD + body[:-2]) is None            # the final empty line not yet in
    assert _resp(_CF_HEAD + b"5\r\nhello\r\n0\r\nX-T: 1\r\n\r\n") == (True, True)   # a trailer
    assert _resp(_CF_HEAD + b"zz\r\n") == (True, False)   # not hex: out of sync, drop
    assert _resp(_CF_HEAD + body + b"HTTP") == (True, False)   # bytes past the end: drop


def test_response_content_length_and_drop_cases():
    ok = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n"
    assert _resp(ok + b"{}") == (True, True)
    assert _resp(ok + b"{") is None
    assert _resp(b"HTTP/1.1 200 OK\r\n\r\n") == (True, True)           # no body at all
    assert _resp(b"HTTP/1.1 200 OK\r\nCONTENT-LENGTH: 0\r\nConnection: close\r\n\r\n") \
        == (True, False)                                              # any case; close drops
    assert _resp(b"HTTP/1.1 200 OK\r\nContent-Length: x\r\n\r\n") == (True, False)
    assert _resp(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n") == (True, False)
    assert _resp(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")[0] is False
    assert _resp(b"HTTP/1.1 200 OK\r\nContent-Le") is None             # headers incomplete


def test_gc_step_is_two_percent_of_the_heap_with_a_floor():
    assert _lib._gc_step(328 * 1024, 0) == 328 * 1024 // 50
    assert _lib._gc_step(64 * 1024, 0) == _lib._GC_MIN
    assert _lib._gc_step(8 << 20, 0) == (8 << 20) // 50
    assert _lib._gc_step(8 << 20, 12345) == 12345                    # configured wins


def test_configure_accepts_gc_bytes():
    old = _lib.limits.gc_bytes
    try:
        _lib.configure(gc_bytes=8192)
        assert _lib.limits.gc_bytes == 8192
    finally:
        _lib.limits.gc_bytes = old
