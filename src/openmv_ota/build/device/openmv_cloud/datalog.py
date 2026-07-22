"""``openmv_cloud.datalog`` -- structured telemetry to the datalake.

    from openmv_cloud import datalog
    datalog.post("imu", {"ax": 1.2, "ay": 0.3, "az": 9.8})

Each posted object is stored as ``{sid, seq, data: <your object>}`` under its
``topic`` in the datalake. ``sid``/``seq`` are managed for you (the backscroll
contract, same as console logs): ``sid`` per boot, ``seq`` monotonic per topic,
so history orders and dedupes without trusting the clock. Your object rides
under ``data`` untouched -- post nested objects, lists, or a bare number.

Persistence-only (no live mirror): telemetry accrues to history, viewed/charted
in the dashboard. Same durability model as :mod:`openmv_cloud.logs` -- RAM-first,
an OPT-IN disk spool (``enable(spool_path=...)``), at-least-once delivery made
safe by the datalake's ``(sid, seq)`` dedup. Idle until an ingest grant arrives
(auto-wired from the OTA check-in); ``post()`` before then just buffers (bounded).

RAM BUDGET: this module runs inside your application, so its memory is your
memory. Topics share ONE byte budget rather than each reserving its own, the
spool writes record by record, and drains read in bounded windows. Keeping many
low-rate topics is the intended use. The ceilings are yours to set; see
``openmv_cloud.configure()``.
"""

import json

from ._lib import (_Conn, _drain_disk, _open_disk, _session_id, _timestamp, budget,
                   limits)

_FLUSH_MS = 5000
_SPOOL_NAME = "openmv_cloud_datalog_%s.ndjson"   # per topic

# One boot session id, shared across topics. Topic names are validated to the
# datalake's charset; "console" is reserved for the logs sink.
_sid = _session_id()
_RESERVED = ("console",)


def _valid_topic(topic):
    if not topic or topic in _RESERVED or len(topic) > 32:
        return False
    for ch in topic:
        if not (ch.islower() or ch.isdigit() or ch in "_-"):
            return False
    return topic[0].islower() or topic[0].isdigit()


def _record(sid, seq, obj, ts=None):
    """One datalake record: the app's object wrapped under ``data`` beside the
    ``(sid, seq)`` key, plus ``ts`` (Unix seconds) when the clock is trustworthy.
    ``ts`` is omitted rather than guessed, so its presence means it is real. Pure."""
    rec = {"sid": sid, "seq": seq, "data": obj}
    if ts is not None:
        rec["ts"] = ts
    return json.dumps(rec).encode()


class _ByteOutbox:
    """Two-tier durable FIFO of pre-encoded record bytes -- the telemetry twin of
    the logs outbox (records already carry ``sid``/``seq``, so no re-encoding).
    RAM-first; when the shared budget says so the whole backlog spills to
    ``disk`` at once. Disk is written only on overflow and drain
    (``write_through`` spills per record). ``disk=None`` -> RAM-only, dropping
    oldest when asked to shed.

    Holds NO cap of its own: every topic is a member of the SDK-wide
    :data:`~openmv_cloud._lib.budget`, which sheds from whichever sink is
    biggest. That is what lets an app keep MANY topics -- an idle topic costs a
    few bytes, and a chatty one can't starve the quiet ones."""

    def __init__(self, disk=None, write_through=False, budget_=None):
        self._disk = disk
        self._write_through = write_through
        self._buf = []                # [record_bytes], oldest first
        self._bytes = 0
        self._budget = budget if budget_ is None else budget_
        self._budget.join(self)

    def add(self, record):
        self._buf.append(record)
        self._bytes += len(record)
        # Charging may shed from the largest member (maybe us), so account first.
        self._budget.charge(len(record))
        if self._disk is not None and self._write_through and self._buf:
            self._spill()

    def shed(self):
        """Give RAM back at the budget's request: spill to disk if we have one,
        else drop our oldest record."""
        if not self._buf:
            return
        if self._disk is not None:
            self._spill()
        else:
            rec = self._buf.pop(0)
            self._bytes -= len(rec)
            self._budget.release(len(rec))

    def _spill(self):
        self._disk.append_iter(self._pieces())
        self._buf = []
        freed, self._bytes = self._bytes, 0
        self._budget.release(freed)

    def _pieces(self):
        """The backlog one record at a time -- the spill never joins the whole
        queue into a single buffer."""
        for rec in self._buf:
            yield rec
            yield b"\n"

    def pending_bytes(self):
        return self._bytes

    def take(self, max_bytes):
        if not self._buf:
            return None
        out, size = [], 0
        while self._buf and (not out or size + len(self._buf[0]) <= max_bytes):
            rec = self._buf.pop(0)
            self._bytes -= len(rec)
            self._budget.release(len(rec))   # in flight: the caller holds it
            out.append(rec)
            size += len(rec)
        return out

    def requeue(self, records):
        self._buf[0:0] = records
        back = sum(len(r) for r in records)
        self._bytes += back
        self._budget.charge(back)            # back on our books; may shed


# --- module state (topics + config + the ingest grant) -----------------------

_topics = {}                          # topic -> {"seq": int, "box": _ByteOutbox}
_ingest = None
_spool_path = None
_write_through = False


def post(topic, obj):
    """Queue ``obj`` as a telemetry record under ``topic`` (validated to
    ``[a-z0-9][a-z0-9_-]{0,31}``, not ``"console"``). Returns True if queued,
    False on a bad topic or once ``limits.topics_max`` topics exist. Cheap and
    sync -- the background flusher uploads it.

    Topics are cheap on purpose: an idle one costs a dict entry and an empty
    list, and they all share ONE byte budget, so "many topics, each at a low
    rate" is the case this is built for. The count cap is not about RAM -- it is
    that every spooled topic is its own FILE on the card."""
    if not _valid_topic(topic):
        return False
    t = _topics.get(topic)
    if t is None:
        if len(_topics) >= limits.topics_max:
            return False
        disk = _open_disk(_spool_path, _SPOOL_NAME % topic)
        t = {"seq": 0, "box": _ByteOutbox(disk=disk, write_through=_write_through)}
        _topics[topic] = t
    t["box"].add(_record(_sid, t["seq"], obj, _timestamp()))
    t["seq"] += 1
    return True


def set_ingest(url, token):
    """Point the sink at the datalake (from the check-in ``ingest`` grant, or
    directly). ``None`` disables uploads (records keep buffering)."""
    global _ingest
    _ingest = (url.rstrip("/"), token) if (url and token) else None


def clear_ingest():
    set_ingest(None, None)


def _on_checkin(resp):
    g = resp.get("ingest")
    if g:
        set_ingest(g.get("url"), g.get("token"))


def _register():  # pragma: no cover  (device: the openmv_ota runtime package)
    try:
        import openmv_ota
        openmv_ota.register_checkin(on_response=_on_checkin, key="openmv_cloud.datalog")
    except (ImportError, AttributeError):
        pass


_register()


def enable(spool_path=None, write_through=False):  # pragma: no cover  (device)
    """Start the background telemetry flusher. ``spool_path`` opts into a durable
    disk spool (per topic), same rules as ``logs.enable`` -- off by default, and
    writing to your card is deliberate. Call once from the app's async world."""
    global _spool_path, _write_through
    _spool_path = spool_path
    _write_through = write_through
    import asyncio
    asyncio.create_task(_flusher())


async def _flusher():  # pragma: no cover  (device loop)
    """Drain every topic: its disk spool first, then its RAM tier. ONE
    :class:`_Conn` serves the whole cycle across ALL topics, so N topics cost
    one ~20 KiB TLS handshake per tick rather than N of them."""
    import asyncio
    while True:
        await asyncio.sleep_ms(_FLUSH_MS)  # type: ignore[attr-defined]
        target = _ingest
        if target is None:
            continue
        batch = limits.batch_bytes
        conn = _Conn(target)
        try:
            for topic, t in list(_topics.items()):
                box = t["box"]
                try:
                    await _drain_disk(conn, topic, box._disk, batch)
                except Exception:
                    continue
                while box.pending_bytes():
                    records = box.take(batch)
                    try:
                        await conn.post(topic, b"\n".join(records))
                    except Exception:
                        box.requeue(records)
                        break
        finally:
            await conn.close()
