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
  watching. Persistence is RAM-first; passing ``enable(spool_path=...)`` opts
  into a two-tier store whose backlog SPILLS to a durable disk spool (e.g.
  ``/sdcard``) on overflow and survives power loss -- OFF by default (spooling
  to the user's card is deliberate, not automatic; RAM-only drops oldest under a
  long outage). Delivery is at-least-once (the datalake's ``(sid, seq)`` dedup
  makes re-sends harmless). Disk is written only on overflow and on drain --
  never per line, since MicroPython doesn't buffer disk writes
  (``write_through=True`` opts into per-line durability, with a performance
  warning). Idle until an
  ingest grant is set.

SEAMLESS BACKSCROLL CONTRACT: every batch is a JSON envelope

    {"sid": "<boot session id>", "seq": <first line number>, "text": "..."}

``seq`` is a per-boot monotonic line counter and ``sid`` identifies the boot
session. The live tail and the (future) datalake copy carry the SAME keys, so
the dashboard terminal can page history with ``(sid, seq < oldest-seen)`` and
stitch it to the live tail with no gaps and no duplicates -- timestamps can't
promise that (RTC jumps, batching); sequence numbers can.

Board level (``openmv_ota.cloud_level()``): on a "no-live" camera there is no
relay stream, no replay ring and no live flusher -- lines persist to the
datalake only. On an "ota-only" camera ``enable()`` does nothing and allocates
nothing: the logger keeps printing wherever ``openmv_log`` sends it.

print() and tracebacks are NOT captured -- only logger records (v1; a dupterm
tee for full-terminal capture is a documented later option).

RAM BUDGET: this module runs inside your application, so its memory is your
memory. Every queue here is byte-capped -- the replay ring, the pending live
batch, and the persistence outbox -- and the spool writes record by record
rather than joining a backlog into one buffer. A stalled network costs a bounded
number of bytes, never unbounded growth. The ceilings are yours to set; see
``openmv_cloud.configure()``.
"""

import json
import logging

from . import csi as _csi          # for csi.Stream only (console as a Live stream)
from ._lib import (_datalake_conn, _drain_disk, _level, _open_disk, _session_id, _start_collector,
                   _timestamp, budget, limits)

_STREAM_NAME = "console"
_FLUSH_MS = 500                   # relay batcher tick while watched
_DATALAKE_FLUSH_MS = 5000         # datalake batcher tick (persistence, not live)
_SPOOL_NAME = "openmv_cloud_console.ndjson"   # this sink's spool file

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


def _line(localtime, ticks_ms, levelname, name, msg):
    """``_format(_stamp(...)) + "\\n"`` in ONE formatting pass. A console line is logged
    every few seconds for as long as the camera runs; building the stamp, the line and then
    the line plus its newline as three strings made two of them garbage at once. Pure; the
    host tests pin it to the frozen formatter's output."""
    if localtime[0] >= 2023:
        return "[%04d-%02d-%02d %02d:%02d:%02d] %s %s: %s\n" % (
            localtime[0], localtime[1], localtime[2], localtime[3], localtime[4], localtime[5],
            levelname, name, msg)
    return "[%5d.%03d] %s %s: %s\n" % (ticks_ms // 1000, ticks_ms % 1000, levelname, name, msg)


def _line_now(levelname, name, msg):  # pragma: no cover  (device clock)
    import time
    return _line(time.localtime(), time.ticks_ms(), levelname, name, msg)


def _envelope(sid, seq, text, ts=None):
    """One relay/datalake console batch -- THE shared backscroll contract, plus
    ``ts`` (Unix seconds) when the clock is trustworthy. ``ts`` is omitted rather
    than guessed, so its presence means it is real."""
    rec = {"sid": sid, "seq": seq, "text": text}
    if ts is not None:
        rec["ts"] = ts
    return json.dumps(rec).encode()


class _Console:
    """The pure console state: a byte-bounded ring of recent ``(seq, line)``
    pairs plus the pending (unsent) batch. Line-granular so the ring never
    tears a line; seq is the per-boot monotonic line counter."""

    def __init__(self, ring_bytes=None, sid=None, live=True):
        self.sid = sid if sid is not None else _session_id()
        # No Live (a "no-live" camera): no viewer will ever be replayed to, so no ring --
        # the console is just the seq counter the datalake records carry.
        self._cap = (ring_bytes if ring_bytes else limits.ring_bytes) if live else 0
        self._seq = 0
        self._ring = []               # [(seq, line str)], newest last
        self._ring_size = 0
        self._pending = []            # [(seq, line/chunk str)] awaiting upload
        self._pending_size = 0        # ...byte-capped like the ring (see _trim_pending)
        self._was_active = False

    def add(self, line, active):
        """A new log line: always into the ring; into the pending batch only
        while watched (unwatched consoles cost memory-bounded ring space, no
        upload, no unbounded queue). Returns the line's ``seq``."""
        return self.push(line, active)[0]

    def push(self, line, active):
        """:meth:`add`, returning the line's ``(seq, line)`` entry -- the one tuple the ring,
        the live batch and the datalake outbox all hold, rather than one each."""
        seq = self._seq
        self._seq += 1
        entry = (seq, line)
        if not self._cap:
            return entry
        self._ring.append(entry)
        self._ring_size += len(line)
        while self._ring_size > self._cap and len(self._ring) > 1:
            self._ring_size -= len(self._ring.pop(0)[1])
        if active:
            self._pending.append(entry)
            self._pending_size += len(line)
            self._trim_pending()
        return entry

    def _trim_pending(self):
        """The live mirror is BEST-EFFORT: if the relay stalls while we're being
        watched, drop the oldest pending lines instead of growing without bound
        (a watched console on a chatty app could otherwise eat the heap while
        flush() keeps failing). The datalake outbox is the durable copy, so a
        dropped line is missing from the live tail only -- not from history."""
        while self._pending_size > self._cap and len(self._pending) > 1:
            self._pending_size -= len(self._pending.pop(0)[1])

    def on_tick(self, active):
        """Called each flusher tick: on the unwatched->watched transition the
        ring is replayed (context for the new viewer), replacing any stale
        pending. Returns ``(first_seq, text)`` to upload, or None."""
        if active and not self._was_active:
            self._pending = list(self._ring)
            self._pending_size = self._ring_size
        self._was_active = active
        if not active or not self._pending:
            return None
        first_seq = self._pending[0][0]
        text = "".join(line for _seq, line in self._pending)
        self._pending = []
        self._pending_size = 0
        return first_seq, text

    def requeue(self, first_seq, text):
        """An upload that couldn't go out yet (send in flight / fps cap): put it
        back so it coalesces into the next batch instead of being lost."""
        self._pending.insert(0, (first_seq, text))
        self._pending_size += len(text)
        self._trim_pending()


def _ndjson(sid, records):
    """Encode ``[(seq, line)]`` as the records of an NDJSON batch of ``{sid, seq, text}`` --
    one per line, so history pages at exact per-line seq granularity. A list: the connection
    joins it into its own reused buffer rather than this allocating the joined batch. The
    datalake requires one sid + non-decreasing seq per batch, which the monotonic console
    counter guarantees."""
    ts = _timestamp()
    return [_envelope(sid, seq, line, ts) for seq, line in records]


class _Outbox:
    """The datalake persistence store -- a TWO-TIER durable FIFO. Recent lines
    live in RAM; on overflow the whole RAM backlog is SPILLED to a disk file
    (``disk``), the older, power-loss-durable tier. One logical oldest->newest
    queue: every disk record is older than every RAM line.

    Writes to disk happen ONLY on overflow (a single append of the whole
    backlog) and on drain -- never per line (MicroPython doesn't buffer disk
    writes, so per-line writes would wreck app performance). Delivery is
    at-least-once; the datalake's ``(sid, seq)`` dedup makes a re-send after a
    crash harmless, so the drain needs no persisted read cursor -- on reboot the
    file replays whole. Disk records carry their ORIGINAL ``sid`` (they belong
    to the boot that wrote them).

    ``disk=None`` -> RAM-only, dropping the oldest when the budget says to (the
    graceful fallback when no writable spool path is available). The only data
    ever lost when a disk IS present is the sub-cap, about-to-send RAM window on
    a sudden power cut -- avoiding even that means write-through, which the
    no-constant-writes rule rules out.

    This outbox holds NO cap of its own: it is a member of the SDK-wide
    :data:`~openmv_cloud._lib.budget`, which sheds from whichever sink is
    largest. So the console competes with the datalog topics for one shared
    pool instead of each reserving its own."""

    def __init__(self, sid=None, disk=None, write_through=False, budget_=None):
        self._sid = sid
        self._disk = disk
        # write_through: spill on EVERY line, not just on overflow -- zero-loss
        # (even the RAM window survives a power cut) at the cost of a disk write
        # per line. Off by default; see enable()'s warning.
        self._write_through = write_through
        self._buf = []                # RAM tier: [(seq, line)], oldest first
        self._bytes = 0
        self._budget = budget if budget_ is None else budget_
        self._budget.join(self)

    def add(self, seq, line):
        self.add_entry((seq, line))

    def add_entry(self, entry):
        """Queue a ``(seq, line)`` entry -- the console's own tuple, shared, not copied."""
        line = entry[1]
        self._buf.append(entry)
        self._bytes += len(line)
        # Charging may push the pool over cap and shed from the largest member
        # -- possibly us -- so append and account BEFORE charging.
        self._budget.charge(len(line))
        if self._disk is not None and self._write_through and self._buf:
            self._spill()             # every line durable, at a disk write each

    def shed(self):
        """Give RAM back at the budget's request: spill the whole backlog to
        disk if we have one (nothing lost) else drop the oldest line."""
        if not self._buf:
            return
        if self._disk is not None:
            self._spill()
        else:
            self._release(self._buf.pop(0)[1])

    def _release(self, line):
        self._bytes -= len(line)
        self._budget.release(len(line))

    def _spill(self):
        # The entire RAM backlog moves to disk in ONE open (encoded with its
        # sid), then RAM clears -- no torn middle. Newline-terminated so
        # consecutive spills stay record-delimited in the file.
        self._disk.append_iter(self._pieces())
        self._buf = []
        freed, self._bytes = self._bytes, 0
        self._budget.release(freed)

    def _pieces(self):
        """The backlog as encoded pieces, one record at a time -- the spill's
        transient stays a single record instead of the whole joined queue."""
        ts = _timestamp()
        for seq, line in self._buf:
            yield _envelope(self._sid, seq, line, ts)
            yield b"\n"

    def pending_bytes(self):
        return self._bytes

    def disk_bytes(self):
        return self._disk.size() if self._disk is not None else 0

    def take(self, max_bytes):
        """Pull the oldest RAM lines up to ``max_bytes`` (at least one) as a
        batch; returns ``[(seq, line)]`` or None when empty. Taken lines leave
        the RAM tier -- the flusher requeues them if the POST fails."""
        if not self._buf:
            return None
        out, size = [], 0
        while self._buf and (not out or size + len(self._buf[0][1]) <= max_bytes):
            seq, line = self._buf.pop(0)
            self._release(line)       # in flight: held by the caller, not by us
            out.append((seq, line))
            size += len(line)
        return out

    def requeue(self, records):
        """A failed RAM POST: put the batch back at the FRONT (oldest). If that
        overflows and a disk is present, the next add() spills it -- so nothing
        is dropped while a spool exists."""
        self._buf[0:0] = records
        back = sum(len(line) for _seq, line in records)
        self._bytes += back
        self._budget.charge(back)     # back on our books; may shed if over cap


class CloudLogHandler(logging.Handler):
    """The bridge from the standard logging tree into both sinks."""

    def __init__(self, console, outbox=None, stamper=None):
        super().__init__()
        self._console = console
        self._outbox = outbox         # datalake persistence (None = live-only)
        self._stamper = stamper       # None: the clock, formatted in the line's own pass
        self.stream = None            # set by enable(); read for live_active

    def emit(self, record):
        # CPython builds the message via getMessage(); MicroPython's logging
        # pre-bakes it into record.message. Support both.
        msg = record.getMessage() if hasattr(record, "getMessage") else record.message
        if self._stamper is None:  # pragma: no cover  (device clock)
            line = _line_now(record.levelname, record.name, msg)
        else:
            line = _format(self._stamper(), record.levelname, record.name, msg) + "\n"
        active = self.stream is not None and self.stream.live_active
        entry = self._console.push(line, active)  # live mirror (ring + relay)
        if self._outbox is not None:
            self._outbox.add_entry(entry)         # persistence (datalake), the same tuple


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
        openmv_ota.register_flush(flush, key="openmv_cloud.logs")
    except (ImportError, AttributeError):
        pass



def enable(level=logging.INFO, logger=None, ring_bytes=None, fps=5,
           spool_path=None, write_through=False):
    """Mirror the logging tree to the cloud: attach the handler (root logger by
    default -- the app's loggers AND openmv_ota's flow through it) and start the
    background flushers (live mirror + datalake persistence). Call once, from
    the app's async world. Returns the handler. ``fps`` caps live batches/sec.

    Persistence is RAM-first and by default NEVER touches storage (a long outage
    drops the oldest lines). Pass ``spool_path`` to opt into a durable disk
    overflow -- e.g. ``spool_path="/sdcard"``; any writable mount works (SD,
    flash-as-disk, SPI-NAND). It's your card, so spooling to it is deliberate,
    not automatic. Disk is then written only on overflow and on drain, never per
    line.

    ``write_through=True`` (meaningful only with a ``spool_path``) writes EVERY
    line to disk immediately, so even the in-RAM window survives a sudden power
    cut -- WARNING: that's a disk write per log line, and MicroPython does not
    buffer disk writes, so it will slow the app noticeably. Off unless zero-loss
    matters more than speed.

    By board level: "no-live" persists only (no relay stream, no ring, no live
    flusher); "ota-only" returns None at once, having allocated nothing."""
    cloud = _level()
    if cloud == "ota-only":
        return None
    return _enable(cloud == "full", level, logger, ring_bytes, fps, spool_path, write_through)


def _enable(live, level, logger, ring_bytes, fps, spool_path,
            write_through):  # pragma: no cover  (device: spawns tasks)
    import asyncio
    disk = _open_disk(spool_path, _SPOOL_NAME)
    if write_through and disk is not None:
        # One-time, BEFORE attaching our handler (so it doesn't self-ingest).
        logging.getLogger("openmv_cloud").warning(
            "logs: write_through on -- a disk write per line; expect slowdown")
    global _kick, _outbox
    console = _Console(ring_bytes if ring_bytes else limits.ring_bytes, live=live)
    outbox = _Outbox(sid=console.sid, disk=disk, write_through=write_through)
    _kick, _outbox = asyncio.Event(), outbox
    handler = CloudLogHandler(console, outbox)
    handler.setLevel(level)
    target = logging.getLogger(logger)
    target.addHandler(handler)
    if target.level > level:      # the root default (WARNING) would eat INFO
        target.setLevel(level)
    # The console as a Live stream -- only where there is Live: a "no-live" camera opens no
    # relay socket for it, so its lines go to the datalake alone.
    stream = _csi.Stream(_STREAM_NAME, fps=fps, encoder=lambda batch, _q: batch) \
        if live else None
    handler.stream = stream

    def ota_handler():
        h = CloudLogHandler(console, outbox)
        h.stream = stream
        return h
    _hear_ota(logging.getLogger("openmv_ota"), ota_handler)
    if stream is not None:
        asyncio.create_task(_flusher(console, stream))
    asyncio.create_task(_datalake_flusher(console.sid, outbox))
    _start_collector()
    return handler


def _hear_ota(ota, make_handler):
    """Let the updater's WARNING and ERROR records reach the cloud console.

    The frozen ``openmv_log`` keeps the ``openmv_ota`` logger OFF (level above CRITICAL) so a
    device with no log sink pays nothing -- which also hid every update failure from the cloud:
    a device offered an update that never installs looked, from the console, exactly like one
    with nothing on offer. WARNING, not INFO: the per-poll chatter stays off, the lines that say
    something is wrong come through. Never LOWERS a level someone set (a bench or debug build
    logging the updater at DEBUG to a UART keeps it).

    Where the records go: a logger with no handler of its own has them handed to the ROOT
    handlers by MicroPython's logging -- this sink, already. But one WITH a handler (the bench's
    UART) does not fall back, so the sink would miss them: then a second cloud handler
    (``make_handler()``, same queues, at WARNING so the UART's DEBUG chatter stays local) is
    attached to it -- once, however often enable() runs.

    No feedback loop: ``CloudLogHandler.emit`` only queues the line, and the flushers that send
    it log nothing; the relay's reconnect warning is one per outage (``csi._loud_reconnect``).
    The installer detaches these handlers again at its erase (``_quiet_past_commit``), because
    past that point their code may be the flash being erased."""
    if ota.level > logging.WARNING:
        ota.setLevel(logging.WARNING)
    if ota.handlers and not any(isinstance(h, CloudLogHandler) for h in ota.handlers):
        h = make_handler()
        h.setLevel(logging.WARNING)
        ota.addHandler(h)


async def _flusher(console, stream):  # pragma: no cover  (device loop)
    import asyncio
    import gc
    stream._ensure_started()
    while True:
        try:
            batch = console.on_tick(stream.live_active)
            if batch is not None:
                first_seq, text = batch
                if not stream.flush(_envelope(console.sid, first_seq, text)):
                    console.requeue(first_seq, text)  # coalesces into the next tick
        except Exception:
            # one bad tick -- a MemoryError building the envelope on a full heap -- must not
            # end the live console for the rest of the boot; NOT logged (it would recurse)
            gc.collect()
        await asyncio.sleep_ms(_FLUSH_MS)  # type: ignore[attr-defined]


# On-demand flush (see flush()): the datalake flusher waits on _kick instead of a bare sleep, and
# counts its finished cycles so a caller can wait for one that started after its kick.
_kick = None              # asyncio.Event, created by enable()
_cycles = 0               # finished datalake cycles
_outbox = None            # enable()'s outbox, for flush() to see what is still queued


def _flushed(start, cycles, pending):
    """Done waiting: a cycle that STARTED after the kick has finished (the one in flight at the
    kick may have missed the newest lines, so it does not count) and nothing is left. Pure."""
    return cycles >= start + 2 or (cycles >= start + 1 and not pending)


async def flush(timeout_ms=10000):  # pragma: no cover  (device: asyncio + clock)
    """Push the console's queued lines to the datalake NOW, waiting at most ``timeout_ms``.
    Returns True when everything queued went out. For a caller about to reset (openmv_ota's
    fresh-heap reboot) -- the normal path ships within one ~5 s tick plus the upload."""
    import asyncio
    import time
    if _kick is None or _ingest is None:
        return False                              # not enabled, or nowhere to send yet
    start = _cycles
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        pending = _outbox.pending_bytes() if _outbox is not None else 0
        if _flushed(start, _cycles, pending):
            return not pending
        _kick.set()
        await asyncio.sleep_ms(100)
    return False


async def _tick(ms):  # pragma: no cover  (device: asyncio)
    """Sleep one flush interval, or less if flush() kicks."""
    import asyncio
    try:
        await asyncio.wait_for_ms(_kick.wait(), ms)
    except asyncio.TimeoutError:
        pass
    _kick.clear()


async def _datalake_flusher(sid, outbox):  # pragma: no cover  (device loop)
    """Push the persistence tiers to the datalake: the disk spool first (oldest,
    records carry their own sid) then the RAM tier. Idle until configured;
    failures leave data in place (nothing lost short of the budget / spool). NOT
    logged -- our handler is on the logging tree, so a warning here would recurse.

    Every batch rides the one shared datalake connection, held open between cycles, so a
    cycle pays no handshake and a long spool drain no more than one. That is what allows a
    small ``batch_bytes`` at no extra cost."""
    global _cycles
    import gc
    while True:
        await _tick(_DATALAKE_FLUSH_MS)
        try:
            await _datalake_cycle(sid, outbox)
        except Exception:
            # A cycle that raises -- outbox.take() is outside the network try, and on a full
            # heap it is a MemoryError -- used to end this task for good: the console went
            # quiet until a reboot while everything else kept running (a Nicla, for hours).
            # The records stay queued; the next tick tries again. NOT logged (recursion).
            gc.collect()
        finally:
            _cycles += 1


async def _datalake_cycle(sid, outbox):  # pragma: no cover  (device network)
    """One flush: the disk spool, then the RAM tier, over the shared datalake connection
    (kept open between cycles -- see ``_lib._datalake_conn``)."""
    target = _ingest
    if target is None:
        return
    batch = limits.batch_bytes
    conn = _datalake_conn(target)
    try:
        await _drain_disk(conn, _STREAM_NAME, outbox._disk, batch)
    except Exception:
        return                                        # network down: retry next tick
    while outbox.pending_bytes():
        records = outbox.take(batch)
        try:
            await conn.post(_STREAM_NAME, _ndjson(sid, records))
        except Exception:
            outbox.requeue(records)
            break


# Wire into openmv_ota last: _register() names functions defined further down.
_register()
