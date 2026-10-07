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
    gc_bytes = 0                  # collect after this much allocation; 0 = 2% of heap (_collector)


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


# --- the board's cloud level ------------------------------------------------

_lvl = None                       # cached cloud level (fixed per firmware)


def _level():
    """This camera's cloud level (``openmv_ota.cloud_level()``): ``"full"``, ``"no-live"`` or
    ``"ota-only"``. Read once and cached -- the firmware fixes it. With no OTA runtime beside
    us, or one that predates the call, it is ``"full"``: the SDK behaves as it always has."""
    global _lvl
    if _lvl is None:
        try:
            import openmv_ota
            _lvl = openmv_ota.cloud_level()
        except (ImportError, AttributeError):
            _lvl = "full"
    return _lvl


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
    """The PEM trust anchors, read once and cached: the same store the OTA runtime
    verifies updates against -- the romfs override ``openmv_ota/data/ca.pem`` if the
    image ships one, else the firmware's frozen copy (``openmv_ota.builtin_ca()``).
    Returns None if the OTA runtime is not installed alongside us (or the firmware
    froze nothing), in which case :func:`_tls_ctx` applies the OTA runtime's rule: refuse,
    except on a firmware built to connect unverified (the M4/M7/H7)."""
    global _ca_pem
    if _ca_pem is None:
        try:
            import openmv_ota
            here = openmv_ota.__file__.rsplit("/", 1)[0]
            try:
                # Read through openmv_ota's helper, which sizes the read by the file rather than
                # by the ceiling: `f.read(_CA_MAX)` pre-allocates 256 KiB in MicroPython and
                # MemoryErrors on any board without external SDRAM (measured on the Nicla
                # Vision). We are already inside `import openmv_ota`, so no new dependency.
                _ca_pem = openmv_ota._read_file(here + "/data/ca.pem", "r", _CA_MAX)
            except OSError:
                # No romfs override shipped (the default): the frozen store, straight
                # out of flash -- no RAM copy.
                _ca_pem = openmv_ota.builtin_ca() or False
        except (ImportError, OSError):
            _ca_pem = False                          # looked, not available
    return _ca_pem or None


def _tls_ctx(ssl):
    """The client context for a relay connection: the OTA runtime's one TLS rule
    (``openmv_ota.tls_configure``) over :func:`_ca`'s anchors -- the same bundle and the same
    behaviour as the check-in. No runtime alongside us is an ImportError: refused, never a
    silent unverified connection."""
    import openmv_ota
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return openmv_ota.tls_configure(ctx, ssl, _ca())


async def _open(host, port, tls):  # pragma: no cover
    import asyncio
    if tls:
        import ssl
        return await asyncio.open_connection(host, port, ssl=_tls_ctx(ssl))
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


_conns = []          # every open datalake connection (see _relieve_conns)


def _hard_close(writer):
    """Close a stream's socket NOW, from synchronous code. MicroPython's asyncio
    ``Stream.close()`` does nothing -- the socket is only closed by ``await wait_closed()`` --
    and the caller here cannot await (it is inside the blocking OTA check-in). Closing the
    socket itself frees its TLS buffers immediately. Never raises."""
    try:
        sock = getattr(writer, "s", None)
        (sock if sock is not None else writer).close()
    except Exception:
        pass


def _relieve_conns(level):
    """The OTA check-in ran out of memory: drop every open datalake connection (at any level --
    a flush is never worth a check-in; its records stay queued and go next tick). Returns how
    many were closed."""
    n = 0
    for conn in list(_conns):
        if conn._writer is not None:
            _hard_close(conn._writer)
            n += 1
        conn._reader = conn._writer = None
        if conn in _conns:
            _conns.remove(conn)
    return n


def _register():  # pragma: no cover  (device: the openmv_ota runtime package)
    try:
        import openmv_ota
        openmv_ota.register_pressure(_relieve_conns, key="openmv_cloud.conns")
    except (ImportError, AttributeError):
        pass


# --- the datalake connection, allocation-free in steady state ------------------
#
# A camera posts its console and telemetry every few seconds, for as long as it runs. Anything
# that request allocates is garbage a few seconds later, and garbage at that rate is what makes
# the heap graph a sawtooth (and, on the small-RAM boards, what fragments the heap until a TLS
# handshake can no longer find its buffers). So the request is written into a reused buffer,
# the response is read into another and parsed in place, and the connection stays open. The
# helpers below are pure so the host tests pin them.

_RESP_BUF = 1536     # a whole response (status, headers, a small body) must fit, else reconnect
_IO_MS = 15000       # one request on the datalake connection, sent and answered, or it is dropped


def _put(buf, off, data):
    """Copy ``data`` into ``buf`` at ``off``; the offset after it. No allocation."""
    end = off + len(data)
    buf[off:end] = data
    return end


def _put_int(buf, off, n):
    """Write the decimal digits of ``n`` (>= 0) into ``buf`` at ``off``; the offset after them.
    By hand: ``str(n).encode()`` would allocate twice per request."""
    start = off
    while True:
        buf[off] = 48 + n % 10
        off += 1
        n //= 10
        if not n:
            break
    i, j = start, off - 1
    while i < j:                                     # the digits went in backwards
        buf[i], buf[j] = buf[j], buf[i]
        i += 1
        j -= 1
    return off


def _num_at(buf, i, end, base):
    """The number in ``base`` (10 or 16) starting at ``buf[i]`` (leading spaces skipped), or -1
    when there is none."""
    while i < end and buf[i] == 32:
        i += 1
    v = -1
    while i < end:
        c = buf[i]
        if 48 <= c <= 57:
            d = c - 48
        elif base == 16 and 97 <= c <= 102:
            d = c - 87
        elif base == 16 and 65 <= c <= 70:
            d = c - 55
        else:
            break
        v = (v if v > 0 else 0) * base + d
        i += 1
    return v


def _chunked_end(buf, pos, n):
    """Where a chunked body starting at ``pos`` ends within ``buf[:n]``: the offset after its
    final empty line, -1 while incomplete, -2 if malformed."""
    while True:
        eol = buf.find(b"\r\n", pos, n)
        if eol < 0:
            return -1
        size = _num_at(buf, pos, eol, 16)
        if size < 0:
            return -2
        if size == 0:
            p = eol + 2
            while True:                              # trailers, then the empty line
                e = buf.find(b"\r\n", p, n)
                if e < 0:
                    return -1
                if e == p:
                    return e + 2
                p = e + 2
        pos = eol + 2 + size + 2
        if pos > n:
            return -1


def _response(buf, n):
    """Where an HTTP response in ``buf[:n]`` stands: None while it is incomplete, else
    ``(ok, reusable)`` -- a 200, and whether the connection is still in sync for the next
    request (a complete body, nothing after it, no ``Connection: close``). Lowercases the header
    block in place so names match whatever case the server or proxy used."""
    h = buf.find(b"\r\n\r\n", 0, n)
    if h < 0:
        return None
    ok = h >= 12 and buf[9] == 50 and buf[10] == 48 and buf[11] == 48     # "HTTP/1.x 200"
    i = 0
    while i < h:
        c = buf[i]
        if 65 <= c <= 90:
            buf[i] = c + 32
        i += 1
    body = h + 4
    keep = buf.find(b"\r\nconnection: close", 0, h) < 0
    if buf.find(b"\r\ntransfer-encoding:", 0, h) >= 0:
        # Cloudflare re-encodes the datalake's reply as chunked: read it, don't reconnect over it
        if buf.find(b"chunked", 0, h) < 0:
            return ok, False
        end = _chunked_end(buf, body, n)
        if end == -1:
            return None
        return ok, keep and end == n
    i = buf.find(b"\r\ncontent-length:", 0, h)
    length = _num_at(buf, i + 17, h, 10) if i >= 0 else 0
    if length < 0:
        return ok, False
    if n < body + length:
        return None
    return ok, keep and n == body + length


def _request_head(buf, pre, topic, mid, length):
    """``POST <base>/<topic> HTTP/1.1 ... Content-Length: <length>`` into ``buf``; its length.
    ``pre`` and ``mid`` are the parts that only change with the connection or the token."""
    off = _put(buf, 0, pre)
    off = _put(buf, off, topic)
    off = _put(buf, off, mid)
    off = _put_int(buf, off, length)
    return _put(buf, off, b"\r\n\r\n")


def _body_len(pieces, sep):
    """The body's length: ``pieces`` joined by ``sep``."""
    n = 0
    for p in pieces:
        n += len(p)
    return n + len(sep) * (len(pieces) - 1) if pieces else 0


def _fill(buf, pieces, sep):
    """``pieces`` joined by ``sep`` into ``buf`` -- the join without the allocation."""
    off = 0
    first = True
    for p in pieces:
        if not first:
            off = _put(buf, off, sep)
        first = False
        off = _put(buf, off, p)
    return off


_shared = None       # the one datalake connection both sinks post on (see _datalake_conn)


def _datalake_conn(target):  # pragma: no cover  (device network)
    """THE datalake connection: one keep-alive socket shared by the console and the datalog
    sinks and held open across their flush cycles. A fresh TLS session costs ~40 KiB of
    short-lived heap (the handshake plus its record buffers); with each sink reconnecting
    every 5 s that was ~8 KiB/s of allocation on an idle camera, measured on an H7 -- the heap
    graph's sawtooth, and the fragmentation that later failed TLS at check-in. Held open, the
    buffers are allocated once and stay put. A renewed grant updates the token in place; a
    moved URL reconnects."""
    global _shared
    if _shared is None:
        _shared = _Conn(target)
    else:
        _shared.retarget(target)
    return _shared


def _write_all(stream, data):  # pragma: no cover  (device: asyncio internals)
    """Send all of ``data`` on an asyncio stream's socket, waiting for room as needed -- what
    ``write()`` + ``drain()`` do, minus their copy: MicroPython's ``Stream.write`` copies
    whatever the socket did not take at once into a new buffer, and TLS takes about one 4 KiB
    record per call. A generator (awaitable), like the asyncio stream methods themselves."""
    from asyncio import core
    s = stream.s
    mv = data if isinstance(data, memoryview) else memoryview(data)
    off, n = 0, len(mv)
    while off < n:
        ret = s.write(mv[off:] if off else mv)
        if ret:
            off += ret
        if off < n:
            yield core._io_queue.queue_write(s)


class _Conn:  # pragma: no cover  (device network)
    """A KEEP-ALIVE HTTP/1.1 connection to the ingest base URL, reused for every
    batch of every flush (see :func:`_datalake_conn`).

    Measured: each fresh TLS handshake allocates ~20 KiB of mbedTLS record
    buffers (IN 16 KiB + OUT 4 KiB; MicroPython does not enable
    MBEDTLS_SSL_VARIABLE_BUFFER_LENGTH, so they stay full size for the
    connection's life) plus the handshake's own transient allocations. Holding
    one connection pays that once per outage instead of once per batch.

    Steady state allocates next to nothing: the request head and body are written into
    ``_out``, sent straight on the socket, and the response is read into ``_resp`` and parsed
    in place (see :func:`_response`) -- one generator per request (:meth:`_exchange`). A response that does not fit, or leaves the
    stream out of sync, drops the socket; the next post reconnects. Every failure closes the
    socket, so a broken one is never reused. A request that takes longer than ``_IO_MS`` is
    cut off by :func:`_watchdog` (``asyncio.wait_for`` would allocate ~0.5 KiB per request).
    Posts are serialized: two sinks share the socket, one request at a time."""

    def __init__(self, target):
        self._url, self._token = target
        self._reader = self._writer = None
        self._used = False
        self._busy = False
        self._owner = None          # the task whose request is in flight
        self._deadline = 0          # ticks_ms by when it must finish
        self._expired = False
        self._mid = None            # request text after the topic, up to Content-Length
        self._topics = {}           # topic -> its bytes, encoded once
        self._out = self._resp = None

    def retarget(self, target):
        """A new grant: the token renews in place; a different URL drops the socket."""
        url, token = target
        if token != self._token:
            self._token = token
            self._mid = None
        if url != self._url:
            self._url = url
            self._drop()

    def _drop(self):
        if self._writer is not None:
            _hard_close(self._writer)
        self._reader = self._writer = None
        self._used = False
        if self in _conns:
            _conns.remove(self)

    async def _connect(self):
        tls, host, port, path = _split_url(self._url)
        self._reader, self._writer = await _open(host, port, tls)
        self._used = False
        self._pre = ("POST %s/" % path.rstrip("/")).encode()
        self._host = host
        self._mid = None
        if self._resp is None:
            self._resp = bytearray(_RESP_BUF)
            self._resp_mv = memoryview(self._resp)
        _conns.append(self)                          # reachable by _relieve_conns

    async def post(self, topic, body):
        """POST one NDJSON batch: ``body`` is the bytes, or a list of records to join with
        newlines (in the reused buffer). Retries once on a REUSED socket -- the server may have
        closed an idle keep-alive connection, which is not an error, just a reconnect. A fresh
        socket failing is real: it raises."""
        import asyncio
        import time
        while self._busy:                            # the other sink's request is in flight
            await asyncio.sleep_ms(20)
        self._busy = True
        self._owner = asyncio.current_task()
        try:
            for attempt in (0, 1):
                if self._reader is None:
                    await self._connect()
                reused = self._used
                self._expired = False
                self._deadline = time.ticks_add(time.ticks_ms(), _IO_MS)
                _start_watchdog()
                try:
                    await self._send(topic, body)
                    return
                except asyncio.CancelledError:
                    if not self._expired:
                        raise                        # somebody else cancelled the task
                    self._drop()
                    raise OSError("datalake request stalled")
                except BaseException as e:
                    self._drop()
                    if attempt or not reused or not isinstance(e, OSError):
                        raise
        finally:
            self._deadline = 0
            self._owner = None
            self._busy = False

    def _prepare(self, topic, body):
        """Request head and body into the reused ``_out`` buffer; how many bytes to send.
        ``body`` is bytes-like, or a list of records to join with newlines."""
        if self._mid is None:
            self._mid = (" HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\nAuthorization: Bearer %s\r\n"
                         "Content-Type: application/x-ndjson\r\nContent-Length: "
                         % (self._host, _UA, self._token)).encode()
        pieces = body if isinstance(body, list) else None
        n = _body_len(pieces, b"\n") if pieces is not None else len(body)
        need = len(self._pre) + len(self._mid) + 32 + 24 + n
        if self._out is None or len(self._out) < need:
            # once per connection in practice: sized for a full batch, not this one
            self._out = bytearray(max(need, len(self._pre) + len(self._mid) + 56
                                      + limits.batch_bytes))
            self._out_mv = memoryview(self._out)
        t = self._topics.get(topic)
        if t is None:
            t = self._topics[topic] = topic.encode()
        head = _request_head(self._out, self._pre, t, self._mid, n)
        if pieces is None:
            _put(self._out, head, body)
        else:
            _fill(self._out_mv[head:], pieces, b"\n")
        return head + n

    def _exchange(self, n):  # a generator: awaited, like the asyncio stream methods
        """Send ``_out[:n]`` and read the whole response into ``_resp``, on the socket
        directly: one generator per request, no copies (``Stream.write`` copies whatever TLS
        does not take at once). Returns ``(ok, reusable)``."""
        from asyncio import core
        s = self._writer.s
        out = self._out_mv
        off = 0
        while off < n:
            ret = s.write(out[off:n])
            if ret:
                off += ret
            if off < n:
                yield core._io_queue.queue_write(s)
        self._used = True
        buf, mv, got = self._resp, self._resp_mv, 0
        while True:
            if got >= len(buf):
                return True, False                   # bigger than we read in place: resync
            yield core._io_queue.queue_read(s)
            r = s.readinto(mv[got:])
            if r is None:
                continue
            if not r:
                raise OSError("datalake closed the connection")
            got += r
            state = _response(buf, got)
            if state is not None:
                if not state[0]:
                    raise OSError("datalake HTTP %s"
                                  % bytes(mv[:min(got, 64)]).split(b"\r\n", 1)[0])
                return state

    async def _send(self, topic, body):
        ok, reusable = await self._exchange(self._prepare(topic, body))
        if not reusable:
            self._drop()

    async def close(self):
        self._drop()


_dog = None          # the watchdog task (see _watchdog)


def _start_watchdog():  # pragma: no cover  (device: spawns a task)
    global _dog
    if _dog is None:
        import asyncio
        _dog = asyncio.create_task(_watchdog())


_gc_task = None      # the collector (see _collector)


def _start_collector():  # pragma: no cover  (device: spawns a task)
    """Start the SDK's garbage collector task, once, whichever sink starts first."""
    global _gc_task
    if _gc_task is None:
        import asyncio
        _gc_task = asyncio.create_task(_collector())


_GC_POLL_MS = 500    # how often the collector looks at the heap
_GC_MIN = 4096       # never collect for less garbage than this


def _gc_step(heap_total, configured):
    """How much allocation the collector lets pile up before it collects: ``configured``
    (``limits.gc_bytes``), else 2% of the heap and at least ``_GC_MIN``. Pure."""
    return configured if configured else max(_GC_MIN, heap_total // 50)


async def _collector():  # pragma: no cover  (device loop)
    """Collect once a little garbage has piled up (:func:`_gc_step`), looking every
    _GC_POLL_MS. Some allocation every frame cannot be avoided -- the camera's own
    ``snapshot()`` returns a new image object each time -- and MicroPython only collects once
    the heap is FULL: until then every allocation walks further up the heap, and whatever
    outlives a collection is left scattered across all of it. That is the heap graph's
    sawtooth, and on the small-RAM boards it shredded the heap until a TLS handshake found no
    8 KiB block with half the heap free (measured on an H7). Collecting a few KiB at a time
    keeps the graph flat (garbage never exceeds ~2% of the heap) and the live objects packed
    at the bottom, so the big blocks stay available. Measured at 3.8 ms per collect on an H7
    with ~190 KiB live; a board that allocates less collects less often."""
    import asyncio
    import gc
    gc.collect()
    total = gc.mem_alloc() + gc.mem_free()
    floor = gc.mem_alloc()
    while True:
        await asyncio.sleep_ms(_GC_POLL_MS)
        a = gc.mem_alloc()
        if a < floor:
            floor = a                                # something else collected
        elif a - floor >= _gc_step(total, limits.gc_bytes):
            gc.collect()
            floor = gc.mem_alloc()


async def _watchdog():  # pragma: no cover  (device loop)
    """Cut off a datalake request that has run past its deadline: drop the socket and cancel
    the task waiting on it (``post`` turns that into an OSError for the flusher). One task for
    the life of the program, so a request costs no timer allocation."""
    import asyncio
    import time
    while True:
        await asyncio.sleep_ms(1000)
        c = _shared
        if c is not None and c._deadline and c._owner is not None \
                and time.ticks_diff(time.ticks_ms(), c._deadline) > 0:
            c._expired = True
            c._deadline = 0
            c._drop()
            c._owner.cancel()


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


_register()
