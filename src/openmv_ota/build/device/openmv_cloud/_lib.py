"""``openmv_cloud._lib`` -- the plumbing every wrapper module shares.

One home for what isn't specific to any single feature: URL parsing, the TLS
connect, the NDJSON ingest POST, the boot session id, record batching (the
datalake's one-sid-per-batch rule), and the durable disk spool tier.

Everything here is feature-agnostic, and dependencies flow one way: the feature
modules import ``_lib``, never each other. (``logs`` does use ``csi.Stream``,
which is a genuine feature dependency -- the console is mirrored as a real Live
stream -- not shared plumbing.)

RAM BUDGET: this module runs inside your application, so its memory is your
memory. Nothing here is sized by a file's length, a response body, or a length
field off the wire: reads use bounded windows, larger data is streamed, and the
sinks share one byte budget. The ceilings are yours to set; see
``openmv_cloud.configure()``.
"""

import json
import os

_UA = "openmv-cam/1.0"            # Cloudflare edge rejects default library UAs
_CHUNK = 4096                     # the universal bounded read/copy window
_SKIP_MAX = 64 * 1024             # give up framing a record after this much
_CA_MAX = 256 * 1024              # the shipped PEM trust bundle
_ca_pem = None                    # cached PEM text (False = looked, absent)
_rtc = None                       # cached openmv_rtc (False = looked, absent)


class _Limits:
    """Every RAM ceiling in the SDK, in one place. These are defaults, not
    policy -- it is your application and your heap, so retune any of them with
    ``openmv_cloud.configure(...)`` before enabling a sink.

    The defaults are sized against the device's TLS cost: mbedTLS allocates
    IN 16 KiB + OUT 4 KiB of record buffers per connection (MicroPython's
    mbedtls_config_common.h, which does not enable variable-length buffers), so
    ~20 KiB is live for the duration of any POST regardless of these settings.
    ``batch_bytes`` matches the 4 KiB TLS *output* record: a larger batch buys no
    wire efficiency, since it is fragmented into 4 KiB records on the way out."""

    budget_bytes = 16 * 1024      # TOTAL RAM buffered across every sink
    batch_bytes = 4 * 1024        # max bytes per ingest POST (= one TLS record)
    ring_bytes = 8 * 1024         # console backlog replayed to a new viewer
    frame_max = 2 * 1024          # ceiling on a relay-declared frame length
    resp_max = 8 * 1024           # ceiling on a response body we read
    topics_max = 32               # datalog topics (each spooled topic = a file)


limits = _Limits()


def configure(**kw):
    """Retune the SDK's RAM knobs (see :class:`_Limits`). Unknown names raise --
    a silently ignored typo would leave the app thinking it had set a budget."""
    for name, value in kw.items():
        if not hasattr(_Limits, name) or name.startswith("_"):
            raise ValueError("unknown limit: " + name)
        if not isinstance(value, int) or value <= 0:
            raise ValueError("%s must be a positive int" % name)
        setattr(limits, name, value)
    budget.cap = limits.budget_bytes
    budget.enforce()                                 # a smaller cap sheds now


# --- the shared RAM budget ---------------------------------------------------

class _Budget:
    """ONE byte budget shared by every buffering sink (the console outbox and
    each datalog topic). Sinks register with :meth:`join` and report
    ``pending_bytes()``; when the total exceeds ``cap`` the LARGEST sink sheds
    first.

    Largest-first is what makes a shared pool workable: with a plain global sum,
    one chatty topic would swallow the pool and starve twenty quiet ones. Max-min
    shedding never touches a small queue while a big one exists, so fairness
    falls out and no sink needs a cap of its own -- which is exactly what "lots
    of topics, each at a low rate" wants."""

    def __init__(self, cap_bytes):
        self.cap = cap_bytes
        self._members = []
        self._total = 0

    def join(self, member):
        if member not in self._members:
            self._members.append(member)

    def leave(self, member):
        if member in self._members:
            self._members.remove(member)

    def total(self):
        return self._total

    def charge(self, n):
        self._total += n
        if self._total > self.cap:
            self.enforce()

    def release(self, n):
        self._total -= n
        if self._total < 0:                          # defensive: never go negative
            self._total = 0

    def enforce(self):
        """Shed from the largest member until we are back under cap."""
        while self._total > self.cap:
            biggest, most = None, 0
            for m in self._members:
                pending = m.pending_bytes()
                if pending > most:
                    biggest, most = m, pending
            if biggest is None:
                return                               # nothing buffered anywhere
            before = self._total
            biggest.shed()
            if self._total >= before:                # shed freed nothing: don't spin
                return


budget = _Budget(_Limits.budget_bytes)


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


def _timestamp():
    """The Unix timestamp to stamp on a record, or None when the clock is not
    trustworthy. ``openmv_rtc`` decides; a device whose RTC never came up simply
    records ``(sid, seq)`` and the server falls back to arrival time, because a
    wrong timestamp is worse than an absent one -- nothing downstream can tell a
    wrong one is wrong."""
    global _rtc
    if _rtc is None:
        try:
            import openmv_rtc
            _rtc = openmv_rtc
        except ImportError:                          # no OTA firmware: no clock
            _rtc = False
    return _rtc.timestamp() if _rtc else None


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

def _ca():  # pragma: no cover  (device: filesystem)
    """The PEM trust anchors, read once and cached: the same bundle the OTA
    runtime verifies updates against (``openmv_ota/data/ca.pem``). Returns None
    if the OTA runtime is not installed alongside us, in which case the platform
    default applies."""
    global _ca_pem
    if _ca_pem is None:
        try:
            import openmv_ota
            here = openmv_ota.__file__.rsplit("/", 1)[0]
            f = open(here + "/data/ca.pem", "r")
            try:
                _ca_pem = f.read(_CA_MAX)
            finally:
                f.close()
        except (ImportError, OSError):
            _ca_pem = False                          # looked, not available
    return _ca_pem or None


async def _open(host, port, tls):  # pragma: no cover
    import asyncio
    if tls:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ca = _ca()
        if ca:
            # Verify the server against the same trust anchors the OTA runtime
            # uses for updates -- one bundle, one behaviour across the device.
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cadata=ca)
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


class _Conn:  # pragma: no cover  (device network)
    """A KEEP-ALIVE HTTP/1.1 connection to the ingest base URL, reused for every
    batch of one flush.

    Measured: each fresh TLS handshake allocates ~20 KiB of mbedTLS record
    buffers (IN 16 KiB + OUT 4 KiB; MicroPython does not enable
    MBEDTLS_SSL_VARIABLE_BUFFER_LENGTH, so they stay full size for the
    connection's life) plus a couple of round trips. Reusing one connection
    across N batches pays that once instead of N times, which is what makes a
    small ``batch_bytes`` cheap: draining a large spool in 4 KiB posts costs the
    same handshake as draining it in 16 KiB posts.

    The response is fully consumed after each POST so the stream stays in sync
    for the next one; anything we can't cheaply resync from (a body over
    ``resp_max``, chunked encoding, ``Connection: close``) just drops the socket
    and the next post reconnects."""

    def __init__(self, target):
        self._url, self._token = target
        self._reader = self._writer = None
        self._used = False

    async def _connect(self):
        tls, host, port, path = _split_url(self._url)
        self._reader, self._writer = await _open(host, port, tls)
        self._host, self._base, self._used = host, path.rstrip("/"), False

    async def post(self, topic, body):
        """POST one NDJSON batch. Retries once on a REUSED socket -- the server
        may have closed an idle keep-alive connection between batches, which is
        not an error, just a reconnect."""
        if self._reader is None:
            await self._connect()
        try:
            await self._send(topic, body)
        except OSError:
            if not self._used:
                raise                                # a fresh socket failing is real
            await self.close()
            await self._connect()
            await self._send(topic, body)

    async def _send(self, topic, body):
        self._writer.write((
            "POST %s/%s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\n"
            "Authorization: Bearer %s\r\nContent-Type: application/x-ndjson\r\n"
            "Content-Length: %d\r\n\r\n"
            % (self._base, topic, self._host, _UA, self._token, len(body))).encode())
        self._writer.write(body)                     # separate write: no body copy
        await self._writer.drain()
        self._used = True
        await self._read_response()

    async def _read_response(self):
        status = await self._reader.readline()
        if b" 200 " not in status and not status.rstrip().endswith(b" 200"):
            await self.close()                       # mid-response: can't reuse
            raise OSError("datalake HTTP %s" % status)
        length, drop = 0, False
        while True:
            line = await self._reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            low = line.lower()
            if low.startswith(b"content-length:"):
                try:
                    length = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    drop = True
            elif low.startswith(b"transfer-encoding:"):
                drop = True                          # chunked: not worth resyncing
            elif low.startswith(b"connection:") and b"close" in low:
                drop = True
        if drop or length > limits.resp_max:
            await self.close()
            return
        left = length                                # consume the body to resync
        while left > 0:
            got = await self._reader.readexactly(left if left < _CHUNK else _CHUNK)
            left -= len(got)

    async def close(self):
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
        self._reader = self._writer = None
        self._used = False


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


async def _drain_disk(conn, topic, disk, max_bytes):  # pragma: no cover  (file+net)
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
            await conn.post(topic, memoryview(window)[:n])
            off += n
    finally:
        disk.compact(off)                            # also clears when fully drained
