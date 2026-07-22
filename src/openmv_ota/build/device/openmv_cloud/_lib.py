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

RAM BUDGET: this runs inside the *user's* app -- our memory is their memory. No
allocation may be sized by something we don't control (a file's size, a response
body, a length field off the wire, a queue that grows while the network is
down). Bounded windows, streaming, and memoryview aliasing instead of copies.
See the RAM budget section in CLAUDE.md.

Pure helpers are host-tested; the socket/filesystem entry points are exercised
on hardware and marked ``# pragma: no cover``.
"""

import json
import os

_UA = "openmv-cam/1.0"            # Cloudflare edge rejects default library UAs
_CHUNK = 4096                     # the universal bounded read/copy window
_SKIP_MAX = 64 * 1024             # give up framing a record after this much


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
    Pure; used for the in-RAM tier, where the record list already exists."""
    sid = _rec_sid(records[start])
    end, size = start, 0
    while end < len(records):
        if end > start and _rec_sid(records[end]) != sid:
            break                                    # a reboot boundary
        n = len(records[end]) + 1                    # +1 for the NDJSON separator
        if end > start and size + n > max_bytes:
            break
        size += n
        end += 1
    return end


def _batch_window(window, max_bytes):
    """How many bytes at the head of ``window`` form ONE complete, single-sid
    NDJSON batch of at most ``max_bytes``; 0 if there is no complete record
    (no newline yet). Pure -- this is the streaming drain's decision function,
    so the drain never needs the file, nor even the batch's record list, in RAM.
    Always takes at least one record, so an oversize record can't wedge it."""
    end = 0                                          # bytes committed so far
    sid = None
    while end < len(window):
        nl = window.find(b"\n", end)
        if nl < 0:
            break                                    # no complete record left
        if end and nl + 1 > max_bytes:
            break                                    # would blow the batch budget
        rec = window[end:nl]
        if rec.strip():                              # blank lines just ride along
            rsid = _rec_sid(rec)
            if sid is None:
                sid = rsid
            elif rsid != sid:
                break                                # a reboot boundary
        end = nl + 1
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


async def _read_capped(reader, limit):  # pragma: no cover  (device network)
    """Read a response body to EOF, capped at ``limit``. Never ``read(-1)``: a
    captive portal or a broken proxy must not be able to size our allocation.
    Collects bounded chunks and joins once (no quadratic ``+=`` growth)."""
    chunks, total = [], 0
    while True:
        d = await reader.read(_CHUNK)
        if not d:
            return b"".join(chunks)
        total += len(d)
        if total > limit:
            raise OSError("response body over %d bytes" % limit)
        chunks.append(d)


async def _post_ndjson(target, topic, body):  # pragma: no cover  (device network)
    """POST already-encoded NDJSON ``body`` to ``{base}/{topic}`` with the ingest
    token. ``target`` is the ``(base_url, token)`` from an ingest grant. Header
    and body are written separately so the body is never copied to concatenate
    it -- callers may hand us a memoryview straight off the spool window."""
    url, token = target
    tls, host, port, path = _split_url(url + "/" + topic)
    reader, writer = await _open(host, port, tls)
    try:
        writer.write((
            "POST %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\n"
            "Authorization: Bearer %s\r\nContent-Type: application/x-ndjson\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n"
            % (path, host, _UA, token, len(body))).encode())
        writer.write(body)
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
    same to us. Append-only during an outage; read in bounded windows and
    compacted (never slurped) on drain -- the file can outgrow RAM by design, so
    nothing here may be sized by ``size()``."""

    def __init__(self, path):
        self._path = path

    def append(self, data):
        f = open(self._path, "ab")
        try:
            f.write(data)
        finally:
            f.close()

    def append_iter(self, pieces):
        """Append many small pieces in ONE open. Lets a caller spill a backlog
        without ever joining it into a single big buffer -- each piece is written
        straight out, so the transient is one record, not the whole queue."""
        f = open(self._path, "ab")
        try:
            for piece in pieces:
                f.write(piece)
        finally:
            f.close()

    def size(self):
        try:
            return os.stat(self._path)[6]
        except OSError:
            return 0

    def read_at(self, off, n):
        """At most ``n`` bytes from ``off`` -- the only read this class offers,
        so no caller can accidentally load the whole spool."""
        f = open(self._path, "rb")
        try:
            f.seek(off)
            return f.read(n)
        finally:
            f.close()

    def clear(self):
        try:
            os.remove(self._path)
        except OSError:
            pass

    def compact(self, off):
        """Drop the first ``off`` bytes, streaming the remainder through a temp
        file in ``_CHUNK`` pieces -- the tail is never held in RAM. Re-reads the
        live size, so records appended while a drain was in flight survive."""
        if off <= 0:
            return
        if off >= self.size():                       # nothing new arrived: done
            self.clear()
            return
        tmp = self._path + ".tmp"
        src = open(self._path, "rb")
        try:
            src.seek(off)
            dst = open(tmp, "wb")
            try:
                while True:
                    chunk = src.read(_CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)
            finally:
                dst.close()
        finally:
            src.close()
        os.remove(self._path)
        os.rename(tmp, self._path)


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


def _skip_record(disk, off, size):  # pragma: no cover  (device: filesystem)
    """Offset just past the next newline at/after ``off``, scanning in bounded
    steps; ``size`` if none within ``_SKIP_MAX``. Only reached for a record too
    big to frame in one batch -- which the datalake would reject anyway -- or a
    torn tail, so skipping it is the one way to keep the spool from wedging."""
    scanned = 0
    while off + scanned < size and scanned < _SKIP_MAX:
        buf = disk.read_at(off + scanned, _CHUNK)
        if not buf:
            break
        nl = buf.find(b"\n")
        if nl >= 0:
            return off + scanned + nl + 1
        scanned += len(buf)
    return size


async def _drain_disk(target, topic, disk, max_bytes):  # pragma: no cover  (file+net)
    """Upload a spool file oldest-first in batches that never mix sids, reading
    ONE batch-sized window at a time -- peak RAM is one batch, whatever the file
    grew to during the outage. Fully sent -> the file goes away; partial ->
    compact off what was sent and stop. A crash mid-drain replays from the last
    compaction; the datalake dedupes by ``(sid, seq)``, so that is harmless."""
    if disk is None:
        return
    size = disk.size()
    if size == 0:
        return
    off = 0
    try:
        while off < size:
            window = disk.read_at(off, max_bytes)
            if not window:
                break
            n = _batch_window(window, max_bytes)
            if n == 0:                               # unframeable record or torn tail
                off = _skip_record(disk, off, size)
                continue
            await _post_ndjson(target, topic, memoryview(window)[:n])
            off += n
    finally:
        disk.compact(off)                            # also clears when fully drained
