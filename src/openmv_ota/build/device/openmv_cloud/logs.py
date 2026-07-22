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

TWO independent sinks, same lines:

* **Live mirror (relay).** Nothing uploads unless someone is watching (the
  relay's ``start``/``stop``). A small RING of recent lines is kept regardless
  and replayed the moment a viewer arrives -- context, not just
  lines-since-join. While watched, lines batch and coalesce.
* **Persistence (datalake).** When an ingest grant is set (:func:`set_ingest`,
  wired by the OTA check-in), EVERY line is also batched to NDJSON and POSTed to
  the datalake -- regardless of viewers, so history exists even when nobody is
  watching. The outbox is memory-bounded (drops oldest under a sustained
  outage; the SD-card spool is the durable-under-outage upgrade later) and
  requeues a failed POST. Configure nothing and this sink is simply idle.

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
_FLUSH_MS = 500                   # relay batcher tick while watched
_OUTBOX_BYTES = 32 * 1024         # datalake outbox cap (drops oldest over this)
_DATALAKE_FLUSH_MS = 5000         # datalake batcher tick (persistence, not live)
_DATALAKE_BATCH_BYTES = 16 * 1024 # POST early once the outbox reaches this
_UA = "openmv-cam/1.0"         # Cloudflare edge rejects default library UAs

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
        upload, no unbounded queue). Returns the line's ``seq`` so the caller
        can also hand it to the datalake outbox."""
        seq = self._seq
        entry = (seq, line)
        self._seq += 1
        self._ring.append(entry)
        self._ring_size += len(line)
        while self._ring_size > self._cap and len(self._ring) > 1:
            self._ring_size -= len(self._ring.pop(0)[1])
        if active:
            self._pending.append(entry)
        return seq

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


def _ndjson(sid, records):
    """Encode ``[(seq, line)]`` as an NDJSON batch of ``{sid, seq, text}``
    records -- one per line, so history pages at exact per-line seq granularity.
    The datalake requires one sid + non-decreasing seq per batch, which the
    monotonic console counter guarantees."""
    return b"\n".join(_envelope(sid, seq, line) for seq, line in records)


class _Outbox:
    """The datalake persistence buffer: a byte-bounded FIFO of ``(seq, line)``.
    Accumulates EVERY line (unlike the watched-only relay pending); the flusher
    drains it to NDJSON POSTs. Bounded so a sustained network outage drops the
    OLDEST lines rather than exhausting the heap (durable-under-outage is the
    later SD-card spool)."""

    def __init__(self, cap_bytes=_OUTBOX_BYTES):
        self._cap = cap_bytes
        self._buf = []                # [(seq, line)], oldest first
        self._bytes = 0

    def add(self, seq, line):
        self._buf.append((seq, line))
        self._bytes += len(line)
        self._trim()

    def _trim(self):
        while self._bytes > self._cap and len(self._buf) > 1:
            self._bytes -= len(self._buf.pop(0)[1])

    def pending_bytes(self):
        return self._bytes

    def take(self, max_bytes):
        """Pull the oldest lines up to ``max_bytes`` (at least one) as a batch;
        returns ``[(seq, line)]`` or None when empty. Taken lines leave the
        outbox -- the flusher requeues them if the POST fails."""
        if not self._buf:
            return None
        out, size = [], 0
        while self._buf and (not out or size + len(self._buf[0][1]) <= max_bytes):
            seq, line = self._buf.pop(0)
            self._bytes -= len(line)
            out.append((seq, line))
            size += len(line)
        return out

    def requeue(self, records):
        """A failed POST: put the batch back at the FRONT (oldest), then re-trim
        -- so a persistent outage still bounds memory by dropping the oldest."""
        self._buf[0:0] = records
        self._bytes += sum(len(line) for _seq, line in records)
        self._trim()


class CloudLogHandler(logging.Handler):
    """The bridge from the standard logging tree into both sinks."""

    def __init__(self, console, outbox=None, stamper=_now_stamp):
        super().__init__()
        self._console = console
        self._outbox = outbox         # datalake persistence (None = live-only)
        self._stamper = stamper
        self.stream = None            # set by enable(); read for live_active

    def emit(self, record):
        # CPython builds the message via getMessage(); MicroPython's logging
        # pre-bakes record.message. NOTE (audit me): confirm on-device field.
        msg = record.getMessage() if hasattr(record, "getMessage") else record.message
        line = _format(self._stamper(), record.levelname, record.name, msg) + "\n"
        active = self.stream is not None and self.stream.live_active
        seq = self._console.add(line, active)     # live mirror (ring + relay)
        if self._outbox is not None:
            self._outbox.add(seq, line)           # persistence (datalake)


# The datalake ingest target, set from the OTA check-in's `ingest` grant. Until
# it's set the persistence sink is idle (the outbox fills, bounded, and drains
# on the first grant).
_ingest = None


def set_ingest(url, token):
    """Point the persistence sink at the datalake: ``url`` is the ingest base
    from the check-in grant (the topic is appended), ``token`` its ingest token.
    Called each check-in so the token renews. ``None`` disables persistence."""
    global _ingest
    _ingest = (url.rstrip("/"), token) if (url and token) else None


def clear_ingest():
    set_ingest(None, None)


def _on_checkin(resp):
    """Pull the ``ingest`` grant out of an OTA check-in response (pure)."""
    g = resp.get("ingest")
    if g:
        set_ingest(g.get("url"), g.get("token"))


def _register():  # pragma: no cover  (device: the openmv_ota runtime package)
    # Auto-wire persistence into openmv_ota.run() so it flows with zero app code.
    try:
        import openmv_ota
        openmv_ota.register_checkin(on_response=_on_checkin, key="openmv_cloud.logs")
    except (ImportError, AttributeError):
        pass


_register()


def enable(level=logging.INFO, logger=None, ring_bytes=_RING_BYTES,
           fps=5):  # pragma: no cover  (device: spawns the flusher tasks)
    """Mirror the logging tree to the cloud: attach the handler (root logger by
    default -- the app's loggers AND openmv_ota's flow through it) and start the
    background flushers (live mirror + datalake persistence). Call once, from
    the app's async world. Returns the handler. ``fps`` caps live batches/sec."""
    import asyncio
    console = _Console(ring_bytes)
    outbox = _Outbox()
    handler = CloudLogHandler(console, outbox)
    handler.setLevel(level)
    target = logging.getLogger(logger)
    target.addHandler(handler)
    if target.level > level:      # the root default (WARNING) would eat INFO
        target.setLevel(level)
    stream = _csi.Stream(_STREAM_NAME, fps=fps, encoder=lambda batch, _q: batch)
    handler.stream = stream
    asyncio.create_task(_flusher(console, stream))
    asyncio.create_task(_datalake_flusher(console.sid, outbox))
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


async def _datalake_flusher(sid, outbox):  # pragma: no cover  (device loop)
    """Persistence loop: while an ingest grant is set, drain the outbox to
    NDJSON POSTs. Idle (no drain) until configured; a failed POST requeues the
    batch so nothing is lost short of the outbox cap."""
    import asyncio
    while True:
        await asyncio.sleep_ms(_DATALAKE_FLUSH_MS)  # type: ignore[attr-defined]
        target = _ingest
        if target is None:
            continue
        while outbox.pending_bytes():
            records = outbox.take(_DATALAKE_BATCH_BYTES)
            try:
                await _post_ndjson(target, sid, records)
            except Exception:
                # Requeue and retry next tick. Deliberately NOT logged: our
                # handler is on the logging tree, so logging here would recurse
                # (emit -> outbox -> fail -> log -> emit...).
                outbox.requeue(records)
                break


async def _post_ndjson(target, sid, records):  # pragma: no cover  (device net)
    """POST one NDJSON batch to ``{base}/{console}`` with the ingest token.
    Reuses the csi module's TLS connect + URL split."""
    url, token = target
    tls, host, port, path = _csi._split_url(url + "/" + _STREAM_NAME)
    reader, writer = await _csi._open(host, port, tls)
    try:
        body = _ndjson(sid, records)
        writer.write((
            "POST %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\n"
            "Authorization: Bearer %s\r\nContent-Type: application/x-ndjson\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n"
            % (path, host, _UA, token, len(body))).encode() + body)
        await writer.drain()
        status = await reader.readline()
        if b" 200 " not in status and not status.rstrip().endswith(b" 200"):
            raise OSError("datalake HTTP %s" % status)
    finally:
        writer.close()
        await writer.wait_closed()
