"""``openmv_cloud._lib`` -- the plumbing every wrapper module shares.

One home for what isn't specific to any single feature: URL parsing, the TLS
connect, the NDJSON ingest POST, the boot session id, record batching (the
datalake's one-sid-per-batch rule), and the durable disk spool tier.

Before this module, ``logs``/``datalog`` imported ``csi`` just to borrow its
networking and ``datalog`` imported ``logs`` just to borrow its spool -- sibling
feature modules reaching into each other for plumbing. Everything here is
feature-agnostic and flows one way: features import ``_lib``, never each other.
(The one legitimate feature dependency that remains is ``logs`` using
``csi.Stream`` to mirror the console as a real Live stream.)

Pure helpers are host-tested; the socket/filesystem entry points are exercised
on hardware and marked ``# pragma: no cover``.
"""

import json
import os

_UA = "openmv-cam/1.0"            # Cloudflare edge rejects default library UAs


# --- URL handling (pure) -----------------------------------------------------

def _split_url(url):
    """``(tls, host, port, path)`` for an http(s)/ws(s) URL. Only what the relay
    grant produces -- no auth/fragment support, and the query string stays in
    ``path`` (the token rides there)."""
    scheme, _, rest = url.partition("://")
    if scheme not in ("http", "https", "ws", "wss"):
        raise ValueError("unsupported url scheme: " + scheme)
    tls = scheme in ("https", "wss")
    hostport, _, tail = rest.partition("/")
    host, _, port = hostport.partition(":")
    if not host:
        raise ValueError("no host in url")
    return tls, host, int(port) if port else (443 if tls else 80), "/" + tail


# --- the backscroll contract: session id + record batching (pure) ------------

def _session_id(rand8=None):
    """The boot session id: 8 random bytes, hex. Distinguishes reboots so the
    backscroll key (sid, seq) stays unambiguous when seq restarts at 0."""
    rand8 = os.urandom(8) if rand8 is None else rand8
    return "".join("%02x" % b for b in rand8)


def _rec_sid(record):
    """The ``sid`` of an encoded record, or None if it isn't a JSON object with a
    string sid (a non-record line packs as its own None-sid run). Pure."""
    try:
        sid = json.loads(record).get("sid")
    except (ValueError, AttributeError):
        return None
    return sid if isinstance(sid, str) else None


def _batch_end(records, start, max_bytes):
    """Index one past the last record of a batch starting at ``start`` that fits
    in ``max_bytes`` (counting the joining newlines); at least one record. Never
    crosses a sid boundary: the datalake requires one sid per batch, and a spool
    that spans a reboot holds runs of different sids (with seq resetting at each).
    Batching by contiguous sid run keeps every batch single-sid and seq-ordered.
    Pure, for the disk drain."""
    sid = _rec_sid(records[start])
    end, size = start, 0
    while end < len(records):
        if end > start and _rec_sid(records[end]) != sid:
            break                                    # a reboot boundary in the spool
        n = len(records[end]) + 1                    # +1 for the NDJSON separator
        if end > start and size + n > max_bytes:
            break
        size += n
        end += 1
    return end


# --- device network plumbing (exercised on hardware, not host) ---------------

async def _open(host, port, tls):  # pragma: no cover
    import asyncio
    if tls:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # NOTE (audit me): server auth should use the bundled CA store (the OTA
        # runtime's data/ca.pem) once the OTA client lands; until wired,
        # MicroPython's default context applies.
        return await asyncio.open_connection(host, port, ssl=ctx)
    return await asyncio.open_connection(host, port)


async def _post_ndjson(target, topic, body):  # pragma: no cover  (device network)
    """POST already-encoded NDJSON ``body`` to ``{base}/{topic}`` with the ingest
    token. ``target`` is the ``(base_url, token)`` from an ingest grant."""
    url, token = target
    tls, host, port, path = _split_url(url + "/" + topic)
    reader, writer = await _open(host, port, tls)
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


# --- the durable spool tier (device filesystem) ------------------------------

class _FileDisk:  # pragma: no cover  (device: filesystem)
    """The durable spool tier over a MicroPython vfs path -- an SD card (e.g.
    the AE3's SPI SD on the battery shield), flash-as-disk, or SPI-NAND, all the
    same to us. Append-only during an outage; read + clear/rewrite on drain."""

    def __init__(self, path):
        self._path = path

    def append(self, data):
        f = open(self._path, "ab")
        try:
            f.write(data)
        finally:
            f.close()

    def size(self):
        try:
            return os.stat(self._path)[6]
        except OSError:
            return 0

    def read_all(self):
        f = open(self._path, "rb")
        try:
            return f.read()
        finally:
            f.close()

    def clear(self):
        try:
            os.remove(self._path)
        except OSError:
            pass

    def rewrite(self, data):
        f = open(self._path, "wb")
        try:
            f.write(data)
        finally:
            f.close()


def _open_disk(spool_path, name):  # pragma: no cover  (device: filesystem)
    """A _FileDisk at ``spool_path/name`` if that's a writable mount, else None
    (which degrades the caller to RAM-only). Each sink passes its own file name
    so spools never collide. Never raises -- a missing or unmounted card must
    not break logging."""
    if not spool_path:
        return None
    try:
        try:
            os.mkdir(spool_path)                     # ensure the dir; ok if it exists
        except OSError:
            pass
        disk = _FileDisk(spool_path.rstrip("/") + "/" + name)
        disk.append(b"")                             # prove it's writable
        return disk
    except OSError:
        return None


async def _drain_disk(target, topic, disk, max_bytes):  # pragma: no cover  (file+net)
    """Upload a spool file oldest-first, in batches that never mix sids. Fully
    sent -> delete the file (back to the RAM-only fast path). A batch failing
    part-way -> rewrite just the un-sent remainder (one write) and stop. A crash
    mid-drain replays the whole file next boot; the datalake dedupes by
    ``(sid, seq)``, so at-least-once delivery is safe."""
    if disk is None or disk.size() == 0:
        return
    records = [r for r in disk.read_all().split(b"\n") if r]
    i = 0
    try:
        while i < len(records):
            end = _batch_end(records, i, max_bytes)
            await _post_ndjson(target, topic, b"\n".join(records[i:end]))
            i = end
    finally:
        if i >= len(records):
            disk.clear()
        elif i > 0:                                   # partial progress persists
            disk.rewrite(b"\n".join(records[i:]))
        # i == 0 (first batch failed): leave the file untouched
