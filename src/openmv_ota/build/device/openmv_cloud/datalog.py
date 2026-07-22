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
"""

import json

from . import csi as _csi
from .logs import _FileDisk, _batch_end, _open_disk, _session_id

_OUTBOX_BYTES = 32 * 1024
_FLUSH_MS = 5000
_BATCH_BYTES = 16 * 1024
_UA = "openmv-cam/1.0"

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


def _record(sid, seq, obj):
    """One datalake record: the app's object wrapped under ``data`` beside the
    ``(sid, seq)`` key -- pure."""
    return json.dumps({"sid": sid, "seq": seq, "data": obj}).encode()


class _ByteOutbox:
    """Two-tier durable FIFO of pre-encoded record bytes -- the telemetry twin of
    the logs outbox (records already carry ``sid``/``seq``, so no re-encoding).
    RAM-first; on overflow the whole backlog spills to ``disk`` at once. Disk is
    written only on overflow and drain (``write_through`` spills per record).
    ``disk=None`` -> RAM-only, dropping oldest over ``cap_bytes``."""

    def __init__(self, cap_bytes=_OUTBOX_BYTES, disk=None, write_through=False):
        self._cap = cap_bytes
        self._disk = disk
        self._write_through = write_through
        self._buf = []                # [record_bytes], oldest first
        self._bytes = 0

    def add(self, record):
        self._buf.append(record)
        self._bytes += len(record)
        if self._disk is not None:
            if self._write_through or self._bytes > self._cap:
                self._spill()
        elif self._bytes > self._cap:
            self._trim()

    def _spill(self):
        self._disk.append(b"\n".join(self._buf) + b"\n")
        self._buf = []
        self._bytes = 0

    def _trim(self):
        while self._bytes > self._cap and len(self._buf) > 1:
            self._bytes -= len(self._buf.pop(0))

    def pending_bytes(self):
        return self._bytes

    def take(self, max_bytes):
        if not self._buf:
            return None
        out, size = [], 0
        while self._buf and (not out or size + len(self._buf[0]) <= max_bytes):
            rec = self._buf.pop(0)
            self._bytes -= len(rec)
            out.append(rec)
            size += len(rec)
        return out

    def requeue(self, records):
        self._buf[0:0] = records
        self._bytes += sum(len(r) for r in records)
        if self._disk is None:
            self._trim()


# --- module state (topics + config + the ingest grant) -----------------------

_topics = {}                          # topic -> {"seq": int, "box": _ByteOutbox}
_ingest = None
_spool_path = None
_write_through = False


def post(topic, obj):
    """Queue ``obj`` as a telemetry record under ``topic`` (validated to
    ``[a-z0-9][a-z0-9_-]{0,31}``, not ``"console"``). Returns True if queued,
    False on a bad topic. Cheap and sync -- the background flusher uploads it."""
    if not _valid_topic(topic):
        return False
    t = _topics.get(topic)
    if t is None:
        disk = _open_disk(_spool_path) if _spool_path else None
        if disk is not None:                          # per-topic spool file
            disk = _FileDisk(_topic_spool(_spool_path, topic))
        t = {"seq": 0, "box": _ByteOutbox(disk=disk, write_through=_write_through)}
        _topics[topic] = t
    t["box"].add(_record(_sid, t["seq"], obj))
    t["seq"] += 1
    return True


def _topic_spool(spool_path, topic):  # pragma: no cover  (device path building)
    return spool_path.rstrip("/") + "/openmv_cloud_datalog_" + topic + ".ndjson"


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
    import asyncio
    while True:
        await asyncio.sleep_ms(_FLUSH_MS)  # type: ignore[attr-defined]
        target = _ingest
        if target is None:
            continue
        for topic, t in list(_topics.items()):
            box = t["box"]
            try:
                await _drain_disk(target, topic, box)
            except Exception:
                continue
            while box.pending_bytes():
                records = box.take(_BATCH_BYTES)
                try:
                    await _post(target, topic, b"\n".join(records))
                except Exception:
                    box.requeue(records)
                    break


async def _drain_disk(target, topic, box):  # pragma: no cover  (device file+net)
    disk = box._disk
    if disk is None or disk.size() == 0:
        return
    records = [r for r in disk.read_all().split(b"\n") if r]
    i = 0
    try:
        while i < len(records):
            end = _batch_end(records, i, _BATCH_BYTES)
            await _post(target, topic, b"\n".join(records[i:end]))
            i = end
    finally:
        if i >= len(records):
            disk.clear()
        elif i > 0:
            disk.rewrite(b"\n".join(records[i:]))


async def _post(target, topic, body):  # pragma: no cover  (device network)
    url, token = target
    tls, host, port, path = _csi._split_url(url + "/" + topic)
    reader, writer = await _csi._open(host, port, tls)
    try:
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
