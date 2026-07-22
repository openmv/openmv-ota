"""Host tests for ``openmv_cloud.logs`` -- the live console mirror.

Pure logic: the (sid, seq) backscroll envelope, the byte-bounded line ring,
watched/unwatched batching, ring replay on viewer arrival, requeue coalescing,
and the logging-tree bridge. The flusher task and enable() wire device asyncio
and are covered on hardware.
"""

from __future__ import annotations

import json
import logging

import pytest

from openmv_ota.build.device.openmv_cloud import logs as lg


def _console(**kw):
    kw.setdefault("sid", "feedc0de00000000")
    return lg._Console(**kw)


@pytest.fixture(autouse=True)
def _reset_ingest():
    lg.clear_ingest()
    yield
    lg.clear_ingest()


# --- envelope: the backscroll contract ------------------------------------------

def test_session_id_is_hex_of_the_random_bytes():
    assert lg._session_id(bytes(range(8))) == "0001020304050607"
    assert len(lg._session_id()) == 16


def test_envelope_shape():
    env = json.loads(lg._envelope("abc123", 42, "line1\nline2\n"))
    assert env == {"sid": "abc123", "seq": 42, "text": "line1\nline2\n"}


# --- the console state ------------------------------------------------------------

def test_lines_are_sequenced_and_ring_is_byte_bounded():
    c = _console(ring_bytes=20)
    for i in range(6):
        c.add("line%d\n" % i, active=False)       # 6 bytes each
    # cap 20 -> only the newest 3 lines fit; seq keeps counting monotonically
    assert [seq for seq, _ in c._ring] == [3, 4, 5]
    assert c._seq == 6


def test_unwatched_lines_never_build_a_pending_queue():
    c = _console()
    c.add("a\n", active=False)
    assert c._pending == []
    assert c.on_tick(active=False) is None


def test_viewer_arrival_replays_the_ring_with_original_seqs():
    c = _console()
    c.add("old1\n", active=False)
    c.add("old2\n", active=False)
    got = c.on_tick(active=True)                  # unwatched -> watched transition
    assert got == (0, "old1\nold2\n")
    assert c.on_tick(active=True) is None         # replayed once, then quiet


def test_watched_lines_batch_and_clear():
    c = _console()
    c.on_tick(active=True)                        # transition consumes empty ring
    c.add("a\n", active=True)
    c.add("b\n", active=True)
    assert c.on_tick(active=True) == (0, "a\nb\n")
    c.add("c\n", active=True)
    assert c.on_tick(active=True) == (2, "c\n")   # seq of the batch's first line


def test_requeue_coalesces_ahead_of_newer_lines():
    c = _console()
    c.on_tick(active=True)
    c.add("a\n", active=True)
    first_seq, text = c.on_tick(active=True)
    c.requeue(first_seq, text)                    # upload couldn't go out
    c.add("b\n", active=True)
    assert c.on_tick(active=True) == (0, "a\nb\n")  # merged, order kept


def test_stale_pending_is_replaced_by_ring_replay_on_rewatch():
    c = _console()
    c.on_tick(active=True)
    c.add("a\n", active=True)
    c.on_tick(active=False)                       # viewer left; pending goes stale
    c.add("b\n", active=False)
    got = c.on_tick(active=True)                  # new viewer: full ring, no dupes
    assert got == (0, "a\nb\n")


# --- the logging bridge -------------------------------------------------------------

class _FakeStream:
    def __init__(self, active):
        self.live_active = active


def _emit(handler, msg):
    rec = logging.LogRecord("app", logging.INFO, __file__, 1, msg, None, None)
    handler.emit(rec)


def test_handler_formats_lines_and_tracks_watched_state():
    c = _console()
    h = lg.CloudLogHandler(c, stamper=lambda: "STAMP")
    _emit(h, "no stream yet")                     # stream unset: unwatched path
    h.stream = _FakeStream(active=True)
    _emit(h, "watched now")
    assert [line for _s, line in c._ring] == [
        "[STAMP] INFO app: no stream yet\n",
        "[STAMP] INFO app: watched now\n",
    ]
    assert [line for _s, line in c._pending] == ["[STAMP] INFO app: watched now\n"]


def test_fallback_formatters_when_frozen_openmv_log_is_absent():
    # On the host the frozen module doesn't import, so the fallbacks are active.
    assert lg._stamp(None, 12345) == "   12.345"
    assert lg._format("S", "INFO", "n", "m") == "[S] INFO n: m"


# --- console.add now returns the seq (fed to the datalake outbox) ---------------------------

def test_console_add_returns_seq():
    c = _console()
    assert c.add("a\n", active=False) == 0
    assert c.add("b\n", active=False) == 1


# --- the datalake outbox (persistence) -----------------------------------------------------

def test_outbox_accumulates_all_lines_regardless_of_watching():
    ob = lg._Outbox(cap_bytes=1000)
    for i in range(4):
        ob.add(i, "line%d\n" % i)
    assert ob.pending_bytes() == sum(len("line%d\n" % i) for i in range(4))
    batch = ob.take(1000)
    assert [seq for seq, _ in batch] == [0, 1, 2, 3]
    assert ob.pending_bytes() == 0
    assert ob.take(1000) is None                 # drained


def test_outbox_is_byte_bounded_drops_oldest():
    ob = lg._Outbox(cap_bytes=20)                 # holds ~3 six-byte lines
    for i in range(6):
        ob.add(i, "line%d\n" % i)                 # 6 bytes each
    seqs = [seq for seq, _ in ob.take(1000)]
    assert seqs == [3, 4, 5]                       # oldest dropped, newest kept


def test_outbox_take_respects_max_bytes_but_always_one():
    ob = lg._Outbox(cap_bytes=1000)
    for i in range(4):
        ob.add(i, "123456\n")                      # 7 bytes each
    first = ob.take(10)                            # only one line fits under 10
    assert [s for s, _ in first] == [0]
    rest = ob.take(1000)
    assert [s for s, _ in rest] == [1, 2, 3]
    # a single oversize line is still taken (never stuck)
    ob.add(9, "x" * 50 + "\n")
    assert [s for s, _ in ob.take(10)] == [9]


def test_outbox_requeue_puts_batch_back_at_front():
    ob = lg._Outbox(cap_bytes=1000)
    ob.add(0, "a\n")
    ob.add(1, "b\n")
    batch = ob.take(1)                             # takes seq 0
    ob.add(2, "c\n")                               # arrives while 0 is "in flight"
    ob.requeue(batch)                              # POST failed -> back to front
    assert [s for s, _ in ob.take(1000)] == [0, 1, 2]


def test_outbox_requeue_re_trims_under_a_persistent_outage():
    ob = lg._Outbox(cap_bytes=14)                 # ~2 seven-byte lines
    ob.add(0, "aaaaaa\n")
    ob.add(1, "bbbbbb\n")
    batch = ob.take(1000)                          # drains both
    ob.add(2, "cccccc\n")                          # new line while POST is out
    ob.requeue(batch)                              # outage: 0,1 back, but over cap
    seqs = [s for s, _ in ob.take(1000)]
    assert seqs[-1] == 2 and len(seqs) == 2        # oldest dropped, bounded


# --- NDJSON encoding (the datalake batch body) ---------------------------------------------

def test_ndjson_one_record_per_line():
    body = lg._ndjson("aa00", [(0, "one\n"), (1, "two\n")])
    # embedded newlines inside `text` are JSON-escaped, so the only real \n
    # bytes are the NDJSON record separators -> safe line-based parsing.
    recs = [json.loads(x) for x in body.split(b"\n") if x.strip()]
    assert recs == [{"sid": "aa00", "seq": 0, "text": "one\n"},
                    {"sid": "aa00", "seq": 1, "text": "two\n"}]


# --- the handler now feeds BOTH sinks ------------------------------------------------------

def test_handler_feeds_console_and_outbox():
    c = _console()
    ob = lg._Outbox()
    h = lg.CloudLogHandler(c, ob, stamper=lambda: "S")
    h.stream = _FakeStream(active=False)
    _emit(h, "hello")
    assert [line for _s, line in c._ring] == ["[S] INFO app: hello\n"]
    assert [line for _s, line in ob.take(1000)] == ["[S] INFO app: hello\n"]


def test_handler_without_outbox_is_live_only():
    c = _console()
    h = lg.CloudLogHandler(c, None, stamper=lambda: "S")
    h.stream = _FakeStream(active=False)
    _emit(h, "hi")                                 # no outbox: must not raise
    assert len(c._ring) == 1


# --- set_ingest plumbing -------------------------------------------------------------------

def test_set_ingest_stores_and_clears_target():
    assert lg._ingest is None
    lg.set_ingest("https://data.test/api/v1/ingest/acct/dev/", "tok")
    assert lg._ingest == ("https://data.test/api/v1/ingest/acct/dev", "tok")
    lg.set_ingest("", "tok")                       # falsy -> disabled
    assert lg._ingest is None
    lg.set_ingest("u", "t")
    lg.clear_ingest()
    assert lg._ingest is None


# --- the OTA check-in extension handler ----------------------------------------------------

def test_on_checkin_sets_ingest_from_the_grant():
    lg._on_checkin({"ingest": {"url": "https://data.test/api/v1/ingest/acct/dev",
                               "token": "tok"}})
    assert lg._ingest == ("https://data.test/api/v1/ingest/acct/dev", "tok")


def test_on_checkin_without_ingest_leaves_it_unset():
    lg._on_checkin({"update": False})
    assert lg._ingest is None


# --- the two-tier durable spool ------------------------------------------------------------

class _FakeDisk:
    def __init__(self):
        self.data = b""
    def append(self, d):
        self.data += d
    def size(self):
        return len(self.data)
    def read_all(self):
        return self.data
    def clear(self):
        self.data = b""
    def rewrite(self, d):
        self.data = d


def _records(disk):
    return [json.loads(x) for x in disk.data.split(b"\n") if x.strip()]


def test_batch_end_packs_records_within_max_bytes():
    recs = [b"12345", b"6789", b"abc"]                # +1 newline each when joined
    assert lg._batch_end(recs, 0, 100) == 3           # all fit
    assert lg._batch_end(recs, 0, 6) == 1             # only the first (5+1)
    assert lg._batch_end(recs, 0, 1) == 1             # oversize still takes one
    assert lg._batch_end(recs, 1, 6) == 2             # from the middle


def _env(sid, seq):
    return lg._envelope(sid, seq, "x\n")


def test_batch_end_never_crosses_a_sid_boundary():
    # a spool spanning a reboot: two sid runs, seq resets. A byte budget that
    # would swallow both runs must still stop at the boundary (one sid per batch).
    recs = [_env("aa00", 0), _env("aa00", 1), _env("bb11", 0), _env("bb11", 1)]
    assert lg._batch_end(recs, 0, 10_000) == 2        # stops at the reboot boundary
    assert lg._batch_end(recs, 2, 10_000) == 4        # the second run drains next


def test_rec_sid_of_a_non_record_line_is_none():
    assert lg._rec_sid(b"12345") is None              # bare int
    assert lg._rec_sid(b"not json") is None
    assert lg._rec_sid(lg._envelope("aa00", 0, "x\n")) == "aa00"


def test_overflow_spills_the_whole_backlog_to_disk_and_clears_ram():
    disk = _FakeDisk()
    ob = lg._Outbox(sid="aa00", cap_bytes=20, disk=disk)
    for i in range(4):
        ob.add(i, "line%d\n" % i)                     # 6 bytes each -> spills past 20
    # RAM cleared on spill; every line is durable on disk, in order, with the sid
    assert ob.pending_bytes() < 20
    recs = _records(disk)
    assert [r["seq"] for r in recs][:4] == [0, 1, 2, 3]
    assert all(r["sid"] == "aa00" for r in recs)


def test_disk_records_keep_their_own_sid_across_a_reboot():
    disk = _FakeDisk()
    old = lg._Outbox(sid="oldsid00", cap_bytes=1, disk=disk)   # a previous boot
    old.add(0, "x\n")                                 # over cap -> spill with old sid
    # a "new boot" reuses the SAME disk file but a new sid
    new = lg._Outbox(sid="newsid11", cap_bytes=1, disk=disk)
    new.add(0, "z\n")
    sids = {r["sid"] for r in _records(disk)}
    assert sids == {"oldsid00", "newsid11"}           # each boot's lines keep their sid


def test_write_through_spills_every_line():
    disk = _FakeDisk()
    ob = lg._Outbox(sid="aa00", cap_bytes=1_000_000, disk=disk, write_through=True)
    ob.add(0, "one\n")
    ob.add(1, "two\n")
    assert ob.pending_bytes() == 0                     # nothing left in RAM
    assert [r["seq"] for r in _records(disk)] == [0, 1]


def test_no_disk_is_ram_only_drop_oldest():
    ob = lg._Outbox(sid="aa00", cap_bytes=20, disk=None)
    for i in range(6):
        ob.add(i, "line%d\n" % i)
    assert ob.disk_bytes() == 0
    assert [s for s, _ in ob.take(1000)] == [3, 4, 5]  # oldest dropped, no spill


def test_requeue_over_cap_with_disk_present_does_not_drop():
    disk = _FakeDisk()
    ob = lg._Outbox(sid="aa00", cap_bytes=10, disk=disk)
    ob.requeue([(0, "a" * 8 + "\n"), (1, "b" * 8 + "\n")])   # 18 bytes > cap 10
    # with a disk present, requeue keeps everything (next add spills); no drop
    assert ob.pending_bytes() == 18
