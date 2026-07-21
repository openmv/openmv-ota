"""``openmv_cloud.csi`` -- the async camera module, with OpenMV Live built in.

``openmv-ota project new --ota`` scaffolds this into ``app/lib/openmv_cloud/``;
it runs under MicroPython on the camera. To the app it IS the camera module: a
drop-in for the builtin ``csi`` (same constructor, same constants, every method
delegated), except ``snapshot()`` is async and OpenMV Live rides along invisibly.
The design rule is *async by default, machinery invisible* -- the app writes
normal camera code and never sees the relay:

    import asyncio
    from openmv_cloud import csi          # instead of: import csi

    csi0 = csi.CSI()
    csi0.reset()
    csi0.pixformat(csi.RGB565)            # constants pass through to the builtin
    csi0.framesize(csi.VGA)

    async def main():
        while True:
            img = await csi0.snapshot()   # the AsyncCSI pattern
            ...                           # the app's CV code, untouched
    asyncio.run(main())

Frame economics -- ZERO-COPY by design (heap fragmentation is the enemy on
MicroPython, so this module never allocates a frame-sized buffer):

* JPEG encoding uses ``img.to_jpeg()`` which converts IN PLACE, at *disposal*
  time -- on entry to the next ``snapshot()``, when the previous frame is being
  recycled anyway (exactly the builtin's lifecycle), or on an explicit
  :meth:`CSI.flush` (call it before deep sleep so the last frame isn't lost).
  The app's ``img`` is never touched while the app still owns it.
* Each stream owns ONE upload buffer, sized DYNAMICALLY to reality: the first
  flush allocates to that frame's size plus headroom (rounded to 4 KiB), and
  because the framesize is constant every following frame reuses it. It
  reallocates only when a frame doesn't fit (busier scene, bigger framesize)
  or the buffer has become 2x oversized (smaller framesize/quality) -- with
  headroom + hysteresis it converges fast, and then steady-state streaming
  allocates nothing frame-sized, ever. No JPEG math, no waste.
  (Pass a fixed ``bufsize=`` instead to cap it: oversize frames then drop with
  a one-time warning -- raise ``bufsize=`` or lower ``quality=``.)
* ``flush`` memcpys the in-place JPEG into that buffer -- sub-millisecond on
  these boards -- so the frame buffer is released IMMEDIATELY; the upload never
  gates the camera pipeline. While an upload is in flight flushes drop (the
  camera's fps is high, a fresh frame follows right after the send drains),
  and the WebSocket send is copy-free (a zero mask key is RFC-legal masking,
  so the buffer goes out unmodified).
* ``flush(img)`` therefore always CONSUMES the image. An app that wants to keep
  using an image after streaming it does its own out-of-place compress + copy
  *before* flushing -- the app controls its allocations, not this module.

Multi-camera and virtual streams: every :class:`CSI` owns one named
:class:`Stream` (name from ``cid``/``stream=``), and a board with several sensors
just creates several ``CSI`` objects. A :class:`Stream` can also be constructed
bare -- a *virtual* stream -- and fed any image via ``flush(img)``; create as
many as you like, each named by its ``stream`` argument:

    overlay = csi.Stream("overlay")       # annotated copies of the main view
    roi = csi.Stream("roi")               # a cropped detail stream
    ...
    overlay.flush(annotated); roi.flush(crop)   # each consumes its image

The module tracks every stream name; the OTA client reports :func:`streams` at
check-in and the response's per-stream grant
(``live.streams.<name>.camera_url/poll_url``) is stored via :func:`set_grant` --
each stream picks up its own URLs. One device credential covers every stream.

Deep-sleep contract: a stream only uploads while the relay says someone is
watching (``start``/``stop``). ``csi0.live_active`` is the one-line sleep gate; a
waking camera asks "anyone watching?" for one HTTPS GET via :func:`poll_watch`
before deciding to stay up.

Like the OTA runtime, the pure logic here (WebSocket codec, handshake, URL
parsing, throttling, the control-message state machine, the stream registry) is
host-testable and the network/device entry points are exercised on hardware. The
WebSocket client is self-contained (MicroPython has none): RFC 6455, client
frames masked, text frames are relay control JSON, binary is never received
(viewers can't publish).
"""

import binascii
import json
import os
import struct

try:
    from openmv_log import log
except ImportError:               # host, or a build without the frozen logger
    class _NullLog:
        def debug(self, msg, *a):
            pass

        def info(self, msg, *a):
            pass

        def warning(self, msg, *a):
            pass

        def error(self, msg, *a):
            pass

    log = _NullLog()

# The relay rejects default library user-agents at the edge (Cloudflare bot
# protection) -- every request this module makes MUST carry a real UA.
_UA = "openmv-camera/1.0"

_DEFAULT_FPS = 5                  # upload cap while watched; ~1-2 Mbit/s at VGA
_DEFAULT_STREAM = "0"
_RECONNECT_BACKOFF_S = 5
_CLOSE_REPLACED = 1012            # relay: a newer camera socket took the room


def __getattr__(name):
    """Constant pass-through (PEP 562): ``csi.RGB565``, ``csi.VGA``, ... come
    straight from the builtin ``csi`` module, so this module is a drop-in import.
    NOTE (audit me): needs MicroPython with module-__getattr__ support; if a
    target port lacks it, replace with explicit constant re-exports."""
    import csi as _builtin
    return getattr(_builtin, name)


# --- the grant (module state; the OTA client refreshes it every check-in) ----

_grant = None


def set_grant(grant):
    """Store the ``live`` object from a check-in response (or None): a per-stream
    map of ready-made URLs sharing one device token. Called by the OTA client on
    every check-in, so the token renews long before its 24 h TTL; an app without
    the OTA loop can call it directly. Streams pick the new grant up on their
    next connect -- an in-flight connection keeps its (still valid) token."""
    global _grant
    _grant = grant if grant else None


def clear_grant():
    set_grant(None)


def _stream_grant(name):
    """This stream's ``{"camera_url", "poll_url"}`` from the current grant, or
    None (no grant yet / the server didn't grant this stream name)."""
    if not _grant:
        return None
    return (_grant.get("streams") or {}).get(name)


# --- the stream registry (what the OTA client reports at check-in) -------------

_streams = {}


def streams():
    """Every live stream name created so far -- the list the OTA client reports
    in the check-in so the server grants each stream its URLs."""
    return list(_streams)


def _register(stream):
    if stream.name in _streams:
        raise ValueError("stream name already in use: " + stream.name)
    _streams[stream.name] = stream


# --- URL handling (pure) ------------------------------------------------------

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


# --- WebSocket codec (pure; RFC 6455) ----------------------------------------

_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


def _handshake_key(rand16):
    """Sec-WebSocket-Key from 16 random bytes (b2a_base64 appends a newline)."""
    return binascii.b2a_base64(rand16)[:-1].decode()


def _handshake_request(host, path, key):
    return ("GET %s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "User-Agent: %s\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, _UA, key)).encode()


def _handshake_ok(status_line):
    """True iff the relay accepted the upgrade. We deliberately do NOT verify the
    Sec-WebSocket-Accept digest: it needs SHA-1 (absent from some MicroPython
    builds), and we already authenticate the *server* via TLS -- the digest only
    protects against cache confusion, which TLS rules out."""
    parts = status_line.split(None, 2)
    return len(parts) >= 2 and parts[0].startswith(b"HTTP/1.") and parts[1] == b"101"


def _encode_frame(opcode, payload, mask_key):
    """One client->server frame (FIN set) with REAL masking -- only used for tiny
    control replies (pong, <=125 bytes), where the per-byte Python XOR is fine.
    Frames go zero-copy via _frame_header instead. If a non-zero mask on large
    payloads is ever needed, vectorize the XOR with ulab (numpy-style math on
    bytearrays is available on the target boards) -- never this loop."""
    length = len(payload)
    if length < 126:
        head = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
    elif length < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
    masked = bytearray(payload)
    for i in range(length):
        masked[i] ^= mask_key[i & 3]
    return head + mask_key + masked


def _decode_header(b0, b1):
    """``(fin, opcode, masked, len7)`` from the first two header bytes; ``len7`` of
    126/127 means an extended length follows (16/64-bit big-endian)."""
    return bool(b0 & 0x80), b0 & 0x0F, bool(b1 & 0x80), b1 & 0x7F


_ZERO_MASK = b"\x00\x00\x00\x00"


def _frame_header(opcode, length):
    """Header + zero mask key for a ZERO-COPY frame send: write this, then the
    payload unmodified. A zero mask key is valid RFC 6455 masking (the mask bit
    is set; XOR with zeros is the identity), and masking's purpose -- proxy
    cache poisoning -- is moot inside TLS. This is what lets a multi-KB JPEG
    memoryview go out with no frame-sized allocation (heap fragmentation).
    VERIFIED against the production relay (workerd, 2026-07-21): a 38 KB
    zero-mask frame delivered byte-identical. Re-verify if the relay is ever
    replaced by a non-workerd implementation."""
    if length < 126:
        head = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
    elif length < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
    return head + _ZERO_MASK


# --- upload throttle (pure) ---------------------------------------------------

class _Throttle:
    """At most ``fps`` sends per second, measured with an injected millisecond
    clock (``time.ticks_ms`` on device). Uses a masked diff so tick wraparound
    (MicroPython ticks wrap at 2^30!) measures modular elapsed time."""

    def __init__(self, fps, ticks_ms):
        self._interval = 1000 // fps if fps > 0 else 0
        self._ticks = ticks_ms
        self._last = None

    def ready(self):
        if self._interval == 0:
            return True
        now = self._ticks()
        if self._last is not None and ((now - self._last) & 0x3FFFFFFF) < self._interval:
            return False
        self._last = now
        return True


# --- relay session state machine (pure) ---------------------------------------

class _Session:
    """Tracks what the relay told us. Fed decoded text frames; owns the single
    source of truth for "is anyone watching" so the stream, the sender task, and
    the app's sleep logic can't disagree."""

    def __init__(self):
        self.streaming = False

    def on_text(self, payload):
        """Apply one relay control message; returns its type (or None if the
        message is unparseable -- tolerated, the relay may grow new ones)."""
        try:
            kind = json.loads(payload)["type"]
        except (ValueError, KeyError, TypeError):
            return None
        if kind == "start":
            self.streaming = True
        elif kind == "stop":
            self.streaming = False
        return kind


def _fit_size(n):
    """Dynamic upload-buffer sizing: the frame size plus 1/8th headroom (scene
    jitter shouldn't trigger a realloc), rounded up to a 4 KiB boundary."""
    n += n >> 3
    return (n + 4095) & ~4095


# --- Stream: one live image stream ----------------------------------------------

class Stream:
    """One named live stream: the relay room ``{device}/{name}``.

    A :class:`CSI` owns one and feeds it automatically. Constructed bare it is a
    *virtual* stream -- the app pushes whatever images it likes (an annotated
    frame buffer, a cropped ROI, ...) with :meth:`flush`:

        overlay = csi.Stream("overlay")
        ...
        overlay.flush(annotated_img)   # consumes the image (in-place JPEG)

    ``encoder`` overrides frame encoding: a callable ``(img, quality) ->
    buffer``. The default JPEGs IN PLACE and returns a zero-copy memoryview of
    the image's own buffer; flush() then memcpys that view into the stream's
    preallocated ``bufsize`` buffer and the image is done with -- the fb
    recycles immediately, the upload runs from the stream's own memory.
    NOTE (audit me): confirm to_jpeg(quality=) + .bytearray() zero-copy
    semantics on rt1062/ae3/n6.
    """

    def __init__(self, name, quality=50, fps=_DEFAULT_FPS, encoder=None,
                 bufsize=None):
        self.name = name
        self._quality = quality
        self._fps = fps
        self._encoder = encoder or _default_encoder
        self._session = _Session()
        self._throttle = None         # created with the task (needs device clock)
        # bufsize=None (default): dynamic -- sized to the first frame + headroom,
        # regrown only on a bigger frame, so allocations converge to zero.
        # bufsize=int: a fixed cap, allocated once here; oversize frames drop.
        self._bufsize = bufsize
        self._buf = bytearray(bufsize) if bufsize is not None else None
        self._sending = False         # upload in flight: the buffer is untouchable
        self._frame = None            # latest-only mailbox: a view into _buf
        self._frame_event = None      # asyncio.Event, created with the task
        self._task = None
        self._dropped = 0             # fixed-cap mode: oversize frames (warned once)
        _register(self)

    @property
    def live_active(self):
        """True while the relay says someone is watching (``start``..``stop``).
        The app's deep-sleep gate: don't sleep while True."""
        return self._session.streaming

    def flush(self, img):
        """Encode + queue ``img`` for upload NOW -- and CONSUME it: the default
        encoder JPEGs in place, then the result is memcpy'd into one of the
        stream's preallocated buffers, so the image (and its frame buffer) is
        free the moment this returns -- the upload NEVER gates the camera.
        An app that wants to keep using the image does its own out-of-place
        compress + copy before flushing. Only encodes while watched and under
        the fps cap, so an unwatched flush costs nothing and leaves ``img``
        untouched. Returns True iff the frame was queued (False also when the
        frame exceeds ``bufsize`` -- dropped with a one-time warning). Must be
        called with the asyncio loop running."""
        self._ensure_started()
        if not self._session.streaming:
            return False
        if self._sending:             # upload in flight: drop -- the camera's fps
            return False              # is high, a fresher frame follows shortly
        if self._throttle is not None and not self._throttle.ready():
            return False
        view = self._encoder(img, self._quality)
        n = len(view)
        if self._bufsize is not None:            # fixed cap: never realloc
            if n > len(self._buf):
                self._dropped += 1
                if self._dropped == 1:
                    log.warning("live[%s]: frame dropped (%d bytes; raise bufsize= "
                                "or lower quality=)" % (self.name, n))
                return False
        elif self._buf is None or n > len(self._buf) \
                or _fit_size(n) < (len(self._buf) >> 1):
            # Dynamic: reallocate when the frame doesn't fit OR the buffer has
            # become 2x oversized for what the stream now produces (framesize or
            # quality dropped). Headroom + 4 KiB rounding + the 2x shrink
            # hysteresis mean this converges and then never runs again.
            self._buf = bytearray(_fit_size(n))
        self._buf[:n] = view          # memcpy: the image is free after this line
        self._frame = memoryview(self._buf)[:n]
        if self._frame_event is not None:
            self._frame_event.set()
        return True

    # -- internals ---------------------------------------------------------

    def _ensure_started(self):
        if self._task is None:
            self._start()

    def _start(self):  # pragma: no cover  (device: spawns the network task)
        import asyncio
        self._frame_event = asyncio.Event()
        self._throttle = _Throttle(self._fps, _ticks_ms())
        self._task = asyncio.create_task(_relay_task(self))

    def _take_frame(self):
        """The sender takes the mailbox view and the buffer becomes in-flight
        (flushes drop) until _release_inflight() -- torn-frame protection.
        asyncio is single-threaded and flush never awaits, so no races."""
        frame, self._frame = self._frame, None
        if frame is not None:
            self._sending = True
        if self._frame_event is not None:
            self._frame_event.clear()
        return frame

    def _release_inflight(self):
        self._sending = False


# --- CSI: the async camera, Live built in ---------------------------------------

class CSI:
    """The app's camera object -- a drop-in for the builtin ``csi.CSI`` that is
    async by default and feeds a :class:`Stream`, hands-free.

    Multi-camera boards create one per sensor; each gets its own stream. The
    stream name is ``stream=`` if given, else the ``cid`` (the builtin's sensor
    selector) as a string, else ``"0"``.
    NOTE (audit me): the builtin's default cid is -1 ("the default sensor") --
    we map missing/-1 to stream "0"; confirm that matches the cid values apps
    actually pass on multi-sensor boards.

    Construction is cheap and network-silent; the machinery starts on the first
    ``await snapshot()``. Frames are encoded at DISPOSAL time: entering
    ``snapshot()`` recycles the previous frame (builtin semantics), which is the
    one safe moment for the in-place ``to_jpeg`` -- the app is done with it.
    Call :meth:`flush` before deep sleep so the final frame isn't lost.
    """

    def __init__(self, *args, cam=None, stream=None, quality=50, fps=_DEFAULT_FPS,
                 encoder=None, **kwargs):
        if cam is None:  # pragma: no cover  (device: needs real csi hardware)
            import csi as _builtin
            cam = _builtin.CSI(*args, **kwargs)
        self._cam = cam
        if stream is None:
            cid = kwargs.get("cid", args[0] if args else -1)
            stream = _DEFAULT_STREAM if cid in (None, -1) else str(cid)
        self._stream = Stream(stream, quality=quality, fps=fps, encoder=encoder)
        self._pending = None          # the frame the app currently owns

    def __getattr__(self, name):
        # Everything we don't implement IS the wrapped camera -- reset(),
        # pixformat(), framesize(), ioctl(), ... the app treats us as the builtin.
        return getattr(self._cam, name)

    @property
    def stream(self):
        return self._stream

    @property
    def live_active(self):
        return self._stream.live_active

    async def snapshot(self, **kwargs):
        """The AsyncCSI pattern: non-blocking capture, yielding to the scheduler
        until a frame is ready. On entry, the PREVIOUS frame is disposed into the
        live stream (encoded in place -- the app is done with it, exactly like
        the builtin recycling its frame buffer)."""
        import asyncio
        self._stream._ensure_started()
        pending, self._pending = self._pending, None
        if pending is not None:
            self._stream.flush(pending)
        while True:
            img = self._cam.snapshot(blocking=False, **kwargs)
            if img is not None:
                self._pending = img
                return img
            await asyncio.sleep_ms(0)  # type: ignore[attr-defined]

    def flush(self):
        """Dispose the last snapshot into the live stream immediately (consumes
        it -- don't use the image afterwards). Call before deep sleep, or when
        the app stops calling snapshot() for a while. Returns True iff a frame
        was queued."""
        pending, self._pending = self._pending, None
        if pending is None:
            return False
        return self._stream.flush(pending)


def _default_encoder(img, quality):  # pragma: no cover  (device: image API)
    # In-place JPEG, then a zero-copy view of the image's own buffer: the ONLY
    # allocation is the memoryview object itself, never a frame-sized buffer.
    return memoryview(img.to_jpeg(quality=quality).bytearray())


def _ticks_ms():  # pragma: no cover  (device)
    import time
    return time.ticks_ms


# --- the wake-cycle check (deep sleep) -----------------------------------------

def parse_poll_response(body):
    """``(watch, viewers)`` from a relay /poll body -- pure, for host tests."""
    obj = json.loads(body)
    return bool(obj["watch"]), int(obj.get("viewers", 0))


async def poll_watch(stream=_DEFAULT_STREAM, grant=None):  # pragma: no cover  (device)
    """One HTTPS GET to the relay: is anyone waiting to watch ``stream``? For the
    deep-sleep wake cycle -- far cheaper than a TLS+WebSocket upgrade when the
    answer is almost always "no, sleep". Returns ``(watch, viewers)``; raises
    OSError on network failure (wake logic should treat that as "sleep, retry
    next wake")."""
    entry = ((grant or {}).get("streams") or {}).get(stream) if grant \
        else _stream_grant(stream)
    if not entry:
        return False, 0
    tls, host, port, path = _split_url(entry["poll_url"])
    reader, writer = await _open(host, port, tls)
    try:
        writer.write(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\n"
                      "Connection: close\r\n\r\n" % (path, host, _UA)).encode())
        await writer.drain()
        status = await reader.readline()
        if b" 200 " not in status and not status.rstrip().endswith(b" 200"):
            raise OSError("poll: HTTP %s" % status)
        while True:                              # skip headers
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        return parse_poll_response(await reader.read(-1))
    finally:
        writer.close()
        await writer.wait_closed()


# --- device network plumbing (exercised on hardware, not host) -----------------

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


async def _ws_connect(url):  # pragma: no cover
    """Open + upgrade a relay WebSocket; returns ``(reader, writer)``."""
    tls, host, port, path = _split_url(url)
    reader, writer = await _open(host, port, tls)
    writer.write(_handshake_request(host, path, _handshake_key(os.urandom(16))))
    await writer.drain()
    status = await reader.readline()
    if not _handshake_ok(status):
        writer.close()
        await writer.wait_closed()
        raise OSError("relay refused upgrade: %s" % status)
    while True:                                  # drain response headers
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
    return reader, writer


async def _ws_recv(reader):  # pragma: no cover
    """One relay->camera frame: ``(opcode, payload)``. Server frames are never
    masked (RFC 6455); extended lengths per _decode_header."""
    head = await reader.readexactly(2)
    _fin, opcode, _masked, len7 = _decode_header(head[0], head[1])
    if len7 == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif len7 == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    else:
        length = len7
    return opcode, await reader.readexactly(length) if length else b""


async def _relay_task(stream):  # pragma: no cover
    """The background machinery: one task per Stream. Waits for this stream's
    grant, holds the relay socket, forwards control messages into the session,
    and pushes the freshest flushed frame while watched. Reconnects forever with
    backoff -- the app never sees a network error, just live_active staying
    False."""
    import asyncio
    while True:
        entry = _stream_grant(stream.name)
        if not entry:
            await asyncio.sleep(_RECONNECT_BACKOFF_S)
            continue
        try:
            reader, writer = await _ws_connect(entry["camera_url"])
            log.info("live[%s]: connected to relay" % stream.name)
            await _pump(stream, reader, writer)
        except Exception as e:
            log.warning("live[%s]: %s; reconnecting" % (stream.name, repr(e)))
        stream._session.streaming = False        # a dead socket streams to no one
        await asyncio.sleep(_RECONNECT_BACKOFF_S)


async def _pump(stream, reader, writer):  # pragma: no cover
    """Run the receive + send halves until the socket dies. Two sub-tasks: recv
    (control messages, ping/pong, close) and send (frames as the mailbox fills)."""
    import asyncio

    async def recv():
        while True:
            opcode, payload = await _ws_recv(reader)
            if opcode == _OP_TEXT:
                stream._session.on_text(payload)
            elif opcode == _OP_PING:
                writer.write(_encode_frame(_OP_PONG, payload, os.urandom(4)))
                await writer.drain()
            elif opcode == _OP_CLOSE:
                return

    async def send():
        while True:
            await stream._frame_event.wait()
            frame = stream._take_frame()
            if frame is not None and stream._session.streaming:
                try:
                    # Copy-free: header (with the RFC-legal zero mask key), then
                    # the buffer view unmodified. The buffer stays in-flight
                    # (unwritable by flush) until the send drains.
                    writer.write(_frame_header(_OP_BINARY, len(frame)))
                    writer.write(frame)
                    await writer.drain()
                finally:
                    stream._release_inflight()

    recv_t = asyncio.create_task(recv())
    send_t = asyncio.create_task(send())
    try:
        await recv_t                             # the relay closing ends the session
    finally:
        send_t.cancel()
        writer.close()
        await writer.wait_closed()
