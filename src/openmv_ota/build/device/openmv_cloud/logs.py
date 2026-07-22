"""``openmv_cloud.logs`` -- live console mirroring over the OpenMV Live relay.

One call in the app's setup:

    import logging
    from openmv_cloud import logs

    logs.enable()                          # that's it
    logging.getLogger("app").info("hi")    # ...and this line is live in the cloud

``enable()`` attaches a handler to the (root) logger -- the standard ``logging``
tree the frozen ``openmv_log`` config also uses, so THE FROZEN MODULE IS NEVER
TOUCHED: handlers are runtime state, no firmware rebuild -- and mirrors every
record to a relay :class:`~openmv_cloud.csi.Stream` named ``"console"``. The
dashboard's terminal pane is just a viewer on that stream rendering text
instead of JPEG.

Economics, same doctrine as video:

* Nothing uploads unless someone is watching (the relay's ``start``/``stop``).
* A small RING of recent lines is kept regardless (bounded, line-granular), and
  is replayed the moment a viewer arrives -- context, not just lines-since-join.
* While watched, lines batch and coalesce: a slow uplink produces bigger
  batches, not lost lines (an unsent batch is requeued, never dropped).
* Text is bytes-per-second, not megabits -- the datalake (persistence,
  retention) is a separate sink this handler grows later; the relay mirror
  never writes to any database.

SEAMLESS BACKSCROLL CONTRACT: every batch is a JSON envelope

    {"sid": "<boot session id>", "seq": <first line number>, "text": "..."}

``seq`` is a per-boot monotonic line counter and ``sid`` identifies the boot
session. The live tail and the (future) datalake copy carry the SAME keys, so
the dashboard terminal can page history with ``(sid, seq < oldest-seen)`` and
stitch it to the live tail with no gaps and no duplicates -- timestamps can't
promise that (RTC jumps, batching); sequence numbers can.

print() and tracebacks are NOT captured -- only logger records (v1; a dupterm
tee for full-terminal capture is a documented later option).
"""

import json
import logging
import os

from . import csi as _csi

_STREAM_NAME = "console"
_RING_BYTES = 8192                # recent-line backlog replayed to a new viewer
_FLUSH_MS = 500                   # batcher tick while watched

try:                              # the frozen formatter helpers, when present
    from openmv_log import _format, _stamp
except ImportError:               # host / no frozen openmv_log: minimal fallback
    def _stamp(localtime, ticks_ms):
        return "%5d.%03d" % (ticks_ms // 1000, ticks_ms % 1000)

    def _format(stamp, levelname, name, msg):
        return "[%s] %s %s: %s" % (stamp, levelname, name, msg)


def _now_stamp():  # pragma: no cover  (device clock)
    import time
    return _stamp(time.localtime(), time.ticks_ms())


def _session_id(rand8=None):
    """The boot session id: 8 random bytes, hex. Distinguishes reboots so the
    backscroll key (sid, seq) stays unambiguous when seq restarts at 0."""
    rand8 = os.urandom(8) if rand8 is None else rand8
    return "".join("%02x" % b for b in rand8)


def _envelope(sid, seq, text):
    """One relay/datalake console batch -- THE shared backscroll contract."""
    return json.dumps({"sid": sid, "seq": seq, "text": text}).encode()


class _Console:
    """The pure console state: a byte-bounded ring of recent ``(seq, line)``
    pairs plus the pending (unsent) batch. Line-granular so the ring never
    tears a line; seq is the per-boot monotonic line counter."""

    def __init__(self, ring_bytes=_RING_BYTES, sid=None):
        self.sid = sid if sid is not None else _session_id()
        self._cap = ring_bytes
        self._seq = 0
        self._ring = []               # [(seq, line str)], newest last
        self._ring_size = 0
        self._pending = []            # [(seq, line/chunk str)] awaiting upload
        self._was_active = False

    def add(self, line, active):
        """A new log line: always into the ring; into the pending batch only
        while watched (unwatched consoles cost memory-bounded ring space, no
        upload, no unbounded queue)."""
        entry = (self._seq, line)
        self._seq += 1
        self._ring.append(entry)
        self._ring_size += len(line)
        while self._ring_size > self._cap and len(self._ring) > 1:
            self._ring_size -= len(self._ring.pop(0)[1])
        if active:
            self._pending.append(entry)

    def on_tick(self, active):
        """Called each flusher tick: on the unwatched->watched transition the
        ring is replayed (context for the new viewer), replacing any stale
        pending. Returns ``(first_seq, text)`` to upload, or None."""
        if active and not self._was_active:
            self._pending = list(self._ring)
        self._was_active = active
        if not active or not self._pending:
            return None
        first_seq = self._pending[0][0]
        text = "".join(line for _seq, line in self._pending)
        self._pending = []
        return first_seq, text

    def requeue(self, first_seq, text):
        """An upload that couldn't go out yet (send in flight / fps cap): put it
        back so it coalesces into the next batch instead of being lost."""
        self._pending.insert(0, (first_seq, text))


class CloudLogHandler(logging.Handler):
    """The bridge from the standard logging tree into the console state."""

    def __init__(self, console, stamper=_now_stamp):
        super().__init__()
        self._console = console
        self._stamper = stamper
        self.stream = None            # set by enable(); read for live_active

    def emit(self, record):
        # CPython builds the message via getMessage(); MicroPython's logging
        # pre-bakes record.message. NOTE (audit me): confirm on-device field.
        msg = record.getMessage() if hasattr(record, "getMessage") else record.message
        line = _format(self._stamper(), record.levelname, record.name, msg) + "\n"
        active = self.stream is not None and self.stream.live_active
        self._console.add(line, active)


def enable(level=logging.INFO, logger=None, ring_bytes=_RING_BYTES,
           fps=5):  # pragma: no cover  (device: spawns the flusher task)
    """Mirror the logging tree to the cloud: attach the handler (root logger by
    default -- the app's loggers AND openmv_ota's flow through it) and start the
    background flusher. Call once, from the app's async world. Returns the
    handler. ``fps`` caps upload batches per second."""
    import asyncio
    console = _Console(ring_bytes)
    handler = CloudLogHandler(console)
    handler.setLevel(level)
    target = logging.getLogger(logger)
    target.addHandler(handler)
    if target.level > level:      # the root default (WARNING) would eat INFO
        target.setLevel(level)
    stream = _csi.Stream(_STREAM_NAME, fps=fps, encoder=lambda batch, _q: batch)
    handler.stream = stream
    asyncio.create_task(_flusher(console, stream))
    return handler


async def _flusher(console, stream):  # pragma: no cover  (device loop)
    import asyncio
    stream._ensure_started()
    while True:
        batch = console.on_tick(stream.live_active)
        if batch is not None:
            first_seq, text = batch
            if not stream.flush(_envelope(console.sid, first_seq, text)):
                console.requeue(first_seq, text)  # coalesces into the next tick
        await asyncio.sleep_ms(_FLUSH_MS)  # type: ignore[attr-defined]
