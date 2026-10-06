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

from openmv_ota.build.device.openmv_cloud import _lib
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
    ob = lg._Outbox(budget_=_lib._Budget(1000))
    for i in range(4):
        ob.add(i, "line%d\n" % i)
    assert ob.pending_bytes() == sum(len("line%d\n" % i) for i in range(4))
    batch = ob.take(1000)
    assert [seq for seq, _ in batch] == [0, 1, 2, 3]
    assert ob.pending_bytes() == 0
    assert ob.take(1000) is None                 # drained


def test_outbox_is_byte_bounded_drops_oldest():
    ob = lg._Outbox(budget_=_lib._Budget(20))                 # holds ~3 six-byte lines
    for i in range(6):
        ob.add(i, "line%d\n" % i)                 # 6 bytes each
    seqs = [seq for seq, _ in ob.take(1000)]
    assert seqs == [3, 4, 5]                       # oldest dropped, newest kept


def test_outbox_take_respects_max_bytes_but_always_one():
    ob = lg._Outbox(budget_=_lib._Budget(1000))
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
    ob = lg._Outbox(budget_=_lib._Budget(1000))
    ob.add(0, "a\n")
    ob.add(1, "b\n")
    batch = ob.take(1)                             # takes seq 0
    ob.add(2, "c\n")                               # arrives while 0 is "in flight"
    ob.requeue(batch)                              # POST failed -> back to front
    assert [s for s, _ in ob.take(1000)] == [0, 1, 2]


def test_outbox_requeue_re_trims_under_a_persistent_outage():
    ob = lg._Outbox(budget_=_lib._Budget(14))                 # ~2 seven-byte lines
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
    """Mirrors _lib._FileDisk: bounded read_at + streaming compact, no read_all."""
    def __init__(self):
        self.data = b""
    def append(self, d):
        self.data += d
    def append_iter(self, pieces):
        for piece in pieces:
            self.data += piece
    def size(self):
        return len(self.data)
    def read_at(self, off, n):
        return self.data[off:off + n]
    def clear(self):
        self.data = b""
    def compact(self, off):
        if off <= 0:
            return
        self.data = b"" if off >= len(self.data) else self.data[off:]


def _records(disk):
    return [json.loads(x) for x in disk.data.split(b"\n") if x.strip()]


def test_overflow_spills_the_whole_backlog_to_disk_and_clears_ram():
    disk = _FakeDisk()
    ob = lg._Outbox(sid="aa00", disk=disk, budget_=_lib._Budget(20))
    for i in range(4):
        ob.add(i, "line%d\n" % i)                     # 6 bytes each -> spills past 20
    # RAM cleared on spill; every line is durable on disk, in order, with the sid
    assert ob.pending_bytes() < 20
    recs = _records(disk)
    assert [r["seq"] for r in recs][:4] == [0, 1, 2, 3]
    assert all(r["sid"] == "aa00" for r in recs)


def test_disk_records_keep_their_own_sid_across_a_reboot():
    disk = _FakeDisk()
    old = lg._Outbox(sid="oldsid00", disk=disk, budget_=_lib._Budget(1))   # a previous boot
    old.add(0, "x\n")                                 # over cap -> spill with old sid
    # a "new boot" reuses the SAME disk file but a new sid
    new = lg._Outbox(sid="newsid11", disk=disk, budget_=_lib._Budget(1))
    new.add(0, "z\n")
    sids = {r["sid"] for r in _records(disk)}
    assert sids == {"oldsid00", "newsid11"}           # each boot's lines keep their sid


def test_write_through_spills_every_line():
    disk = _FakeDisk()
    ob = lg._Outbox(sid="aa00", disk=disk, write_through=True, budget_=_lib._Budget(1_000_000))
    ob.add(0, "one\n")
    ob.add(1, "two\n")
    assert ob.pending_bytes() == 0                     # nothing left in RAM
    assert [r["seq"] for r in _records(disk)] == [0, 1]


def test_no_disk_is_ram_only_drop_oldest():
    ob = lg._Outbox(sid="aa00", disk=None, budget_=_lib._Budget(20))
    for i in range(6):
        ob.add(i, "line%d\n" % i)
    assert ob.disk_bytes() == 0
    assert [s for s, _ in ob.take(1000)] == [3, 4, 5]  # oldest dropped, no spill


def test_requeue_over_budget_with_a_disk_spills_rather_than_drops():
    disk = _FakeDisk()
    ob = lg._Outbox(sid="aa00", disk=disk, budget_=_lib._Budget(10))
    ob.requeue([(0, "a" * 8 + "\n"), (1, "b" * 8 + "\n")])   # 18 bytes > cap 10
    # over budget with a spool: shed by spilling, so RAM is freed and NOTHING is
    # lost -- the records are durable on disk with their seqs intact
    assert ob.pending_bytes() == 0
    assert [r["seq"] for r in _records(disk)] == [0, 1]


# --- the pending batch is byte-capped like the ring -------------------------

def test_pending_is_bounded_when_the_relay_stalls():
    # watched + a chatty app + flush() failing forever must NOT grow without
    # bound: the live mirror is best-effort, the datalake copy is the durable one
    c = _console(ring_bytes=20)
    c.on_tick(active=True)                        # become watched
    for i in range(50):
        c.add("line%d\n" % i, active=True)        # 7 bytes each, never flushed
    assert c._pending_size <= 20
    assert len(c._pending) <= 4


def test_requeue_of_a_huge_batch_is_also_bounded():
    c = _console(ring_bytes=20)
    c.on_tick(active=True)
    c.requeue(0, "x" * 500)                       # a failed upload comes back
    c.add("new\n", active=True)
    assert c._pending_size <= 20                  # trimmed, not accumulated


def test_pending_size_tracks_the_ring_on_viewer_arrival():
    c = _console(ring_bytes=100)
    c.add("a\n", active=False)
    c.add("b\n", active=False)
    c.on_tick(active=True)                        # replay sets pending from ring
    assert c._pending_size == 0                   # ...and the tick drained it


def test_spill_writes_record_by_record_not_one_joined_buffer():
    disk = _FakeDisk()
    ob = lg._Outbox(sid="aa00", disk=disk, budget_=_lib._Budget(20))
    pieces = []
    disk.append_iter = lambda it: pieces.extend(it)   # capture what's handed over
    for i in range(4):
        ob.add(i, "line%d\n" % i)
    # each record is its own piece (plus its separator) -- never one big join
    assert len(pieces) >= 4
    assert all(len(p) < 200 for p in pieces)


def test_shed_on_an_empty_outbox_is_a_noop():
    ob = lg._Outbox(sid="aa00", budget_=_lib._Budget(10))
    ob.shed()                                      # nothing buffered: no error
    assert ob.pending_bytes() == 0


def test_envelope_carries_a_timestamp_only_when_one_is_given():
    # presence of ts MEANS the clock was trustworthy, so it is never defaulted
    assert json.loads(lg._envelope("aa00", 0, "x\n", 1700000000.5))["ts"] == 1700000000.5
    assert "ts" not in json.loads(lg._envelope("aa00", 0, "x\n"))


# --- the updater's warnings reach the cloud console -------------------------------------------

def test_hear_ota_lets_warnings_through_an_off_logger():
    ota = logging.getLogger("test_hear_ota_off")
    ota.setLevel(logging.CRITICAL + 1)              # what the frozen openmv_log sets
    lg._hear_ota(ota, lambda: pytest.fail("no handler of its own: root covers it"))
    assert ota.level == logging.WARNING


def test_hear_ota_never_lowers_a_level_someone_set():
    ota = logging.getLogger("test_hear_ota_debug")
    ota.setLevel(logging.DEBUG)                     # a bench UART at DEBUG keeps it
    lg._hear_ota(ota, lambda: pytest.fail("no handler of its own: root covers it"))
    assert ota.level == logging.DEBUG


def test_hear_ota_attaches_the_sink_to_a_logger_with_its_own_handler():
    # micropython-lib logging does NOT fall back to root handlers when the logger has one (the
    # bench UART), so the cloud sink must sit on the logger itself -- once
    ota = logging.getLogger("test_hear_ota_uart")
    ota.setLevel(logging.DEBUG)
    uart = logging.NullHandler()
    ota.addHandler(uart)
    made = []

    def make():
        h = lg.CloudLogHandler(lg._Console(ring_bytes=1024, sid="s"))
        made.append(h)
        return h
    try:
        lg._hear_ota(ota, make)
        lg._hear_ota(ota, make)                     # enable() again: not duplicated
        assert len(made) == 1 and made[0] in ota.handlers
        assert made[0].level == logging.WARNING     # the UART's DEBUG stays local
        assert ota.level == logging.DEBUG
    finally:
        for h in list(ota.handlers):
            ota.removeHandler(h)


def test_flushed_waits_for_a_cycle_that_started_after_the_kick():
    # the cycle in flight at the kick may have missed the newest line: it alone is not enough
    assert not lg._flushed(start=4, cycles=4, pending=10)
    assert not lg._flushed(start=4, cycles=5, pending=10)      # in-flight one done, lines left
    assert lg._flushed(start=4, cycles=5, pending=0)           # ...but nothing is left: done
    assert lg._flushed(start=4, cycles=6, pending=10)          # a full cycle after: give up waiting


def _run_loop(make, ticks, monkeypatch):
    """Drive a device loop under CPython asyncio for a few ticks: MicroPython's sleep_ms
    becomes a zero sleep, and the loop is cancelled once ``ticks`` cycles have been tried."""
    import asyncio
    monkeypatch.setattr(asyncio, "sleep_ms", lambda ms: asyncio.sleep(0), raising=False)

    async def go():
        task = asyncio.ensure_future(make())
        for _ in range(200):
            await asyncio.sleep(0)
            if ticks():
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())


def test_datalake_flusher_outlives_a_cycle_that_raises(monkeypatch):
    """A cycle that raises (a MemoryError from outbox.take() on a full heap) used to end the
    console uploader for good: the console went quiet until a reboot while telemetry kept
    flowing. The next tick must try again, and every attempt still counts as a cycle."""
    calls = []

    async def cycle(sid, outbox):
        calls.append(sid)
        if len(calls) == 1:
            raise MemoryError("memory allocation failed")

    async def tick(ms):
        import asyncio
        await asyncio.sleep(0)            # a real tick yields; one that never does starves the test
    monkeypatch.setattr(lg, "_datalake_cycle", cycle)
    monkeypatch.setattr(lg, "_tick", tick)
    monkeypatch.setattr(lg, "_cycles", 0)
    _run_loop(lambda: lg._datalake_flusher("s1", None), lambda: len(calls) >= 3, monkeypatch)
    assert len(calls) >= 3 and lg._cycles >= 3


def test_live_console_flusher_outlives_a_tick_that_raises(monkeypatch):
    """The live console's tick survives an error the same way."""
    seen = []

    class _Console:
        sid = "s1"

        def on_tick(self, live):
            seen.append(live)
            if len(seen) == 1:
                raise MemoryError("memory allocation failed")
            return None

    class _Stream:
        live_active = False

        def _ensure_started(self):
            pass
    _run_loop(lambda: lg._flusher(_Console(), _Stream()), lambda: len(seen) >= 3, monkeypatch)
    assert len(seen) >= 3
