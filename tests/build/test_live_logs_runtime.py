"""Host tests for ``openmv_cloud.logs`` -- the live console mirror.

Pure logic: the (sid, seq) backscroll envelope, the byte-bounded line ring,
watched/unwatched batching, ring replay on viewer arrival, requeue coalescing,
and the logging-tree bridge. The flusher task and enable() wire device asyncio
and are covered on hardware.
"""

from __future__ import annotations

import json
import logging

from openmv_ota.build.device.openmv_cloud import logs as lg


def _console(**kw):
    kw.setdefault("sid", "feedc0de00000000")
    return lg._Console(**kw)


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
