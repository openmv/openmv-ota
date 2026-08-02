"""The OTA installer -- fetches a signed manifest over HTTPS, picks the image it points
to, writes that into the FRONT slot, then arms the trial and reboots.

This file ships in the romfs as **source** (it is exempt from the .py->.mpy build
step) so ``openmv_ota.install()`` can ``exec()`` it into RAM before the FRONT slot
is erased: the running app's code lives in that slot, so once the erase starts
nothing on it can be executed -- but this module, compiled into RAM by ``exec``,
runs from RAM throughout. ``run()`` first fetches + verifies the manifest and vets it
(signature, board, anti-rollback) with ``/rom`` intact, then never returns: on success
it sets PENDING and ``machine.reset()``s into the trial; on any post-erase failure it
resets into the golden BACK image (boot.py rejects the half-written FRONT). Pre-erase
failures (bad URL, DNS, TLS, a bad/forbidden manifest) raise normally -- ``/rom`` is
still intact, so the app catches them and can retry without a reboot.

WHY THIS IS SYNCHRONOUS (and must stay so), and how it feeds an enabled watchdog: an app may
turn on ``openmv_wdt``, which this install has to keep fed the whole way through. It runs
SYNCHRONOUSLY -- and MUST, from the erase onward -- because the app's own coroutines (its main
loop, ``run()``, every task on the asyncio event loop) live in the FRONT slot being erased. So
if any post-erase op ``await``\\ed and YIELDED to the event loop, the loop would resume that
now-erased bytecode straight from flash and HardFault. Post-erase work therefore must not yield.
That rules OUT async sockets for the download; instead the recv is **non-blocking + progress-
fed without yielding**: the reader loops a non-blocking ``recv`` and, whenever no data has yet
arrived, ``feed``\\s the watchdog and ``sleep_ms``\\s one short slice before retrying -- a fixed
feed cadence driven from the main thread, with no reliance on ``select.poll`` or a timer (the
AE3's SSL poll() blocks through its own timeout, and relax()'s ISR is not serviced inside an
mbedtls read, so only a main-thread feed is reliable there). A slow/paced link stays fed while a
dead link (no data at all) stops producing and trips the watchdog after ``_SOCK_TIMEOUT`` ->
golden. The erase/write loops feed per block/piece the same way. ``relax()`` (an ISR feed that also does not yield) is a LAST
RESORT, used ONLY for the two genuinely unsplittable single C calls with no seam to feed
through: the TLS handshake (``_connect``) and this file's own ``exec`` compile (in
``openmv_ota.install``). All of it is a no-op unless the app armed a watchdog. (Pre-erase --
check-in, manifest fetch -- could yield safely, and the check-in in ``run()`` does; but the
installer is one synchronous unit for simplicity and the hard post-erase guarantee.)

Like ``boot.py`` this is split into pure logic (URL/HTTP/chunked parsing, the
flash write loop -- all I/O injected, host-tested) and a device entry (``run`` /
``_open`` / ``_connect``) that wires the real ``socket``/``ssl``/``deflate``/
``vfs``/``machine`` and is excluded from host coverage. ``hashlib`` is the only
import that runs on the host (to derive the PENDING marker, pinned against
``openmv_ota.ota.status`` by a test); the device imports are lazy.

RAM BUDGET: this module runs inside your application, so its memory is your
memory. Every buffer here has a ceiling. Nothing is sized by a file's length, a
response body, a length field off the wire, or a queue that grows while the
network is down: reads use bounded windows of a few KB, anything larger is
streamed, and large data is aliased with memoryview/bytearray_at rather than
copied.
"""

import binascii
import hashlib
import io
import json
import struct

try:                                   # the firmware freezes openmv_log beside boot.py
    from openmv_log import log
except ImportError:                    # host / tests / a build without logging -> a null logger,
    class _NullLog:                    # so call sites never need an `is not None` guard
        def debug(self, msg, *a):
            pass

        def info(self, msg, *a):
            pass

        def warning(self, msg, *a):
            pass

        def error(self, msg, *a):
            pass

        def critical(self, msg, *a):
            pass

    log = _NullLog()

try:                                   # ...and openmv_wdt (the watchdog helper)
    import openmv_wdt
except ImportError:
    openmv_wdt = None

try:                                   # device: a real millisecond sleep to yield between recv polls;
    from time import sleep_ms          # host (CPython time has no sleep_ms): a no-op -- the download
except ImportError:                    # reader's wait loop just spins its bounded counter under test.
    def sleep_ms(_ms):
        pass



class _NoWdt:  # pragma: no cover  (fallback relax() context when no watchdog is frozen)
    def __enter__(self):
        return self  # hil-residual: bare return (no-watchdog CM; bench freezes openmv_wdt so the real relax() runs)

    def __exit__(self, *args):
        return False  # hil-residual: bare const return (no-watchdog CM exit)


def _noop():  # pragma: no cover  (fallback feed() when no watchdog is frozen)
    pass  # hil-residual: bare pass (no-watchdog feed; bench freezes openmv_wdt so the real feed() runs)

# --- Status markers (mirror of openmv_ota.ota.status; pinned by a test) ------

MARKER_SIZE = 16
_REPR_OFF = 48                                    # status-sector offset of the repr marker


def _marker(label):
    return hashlib.sha256(b"openmv-ota.status." + label).digest()[:MARKER_SIZE]


PENDING = _marker(b"pending")
REPR_FULL = _marker(b"repr.full")
REPR_DELTA = _marker(b"repr.ocdl")

# Stream/flash unit. FRONT_SIZE is always a multiple of this (it is block-aligned
# and the block is >= 4096), so every flash write is a full, aligned chunk.
_CHUNK = 4096
# Preallocated, immutable compare buffers (memoryview-sliced, never copied). The streamed delta
# apply runs millions of "is this chunk all-zero / all-0xFF?" tests; comparing against a fresh
# bytes(n) each time was a big slice of the delta's heap churn -> GC pressure -> (under a watchdog)
# an unfeedable collection pause. The whole write path now reuses fixed buffers so no per-chunk
# allocation happens and automatic GC never fires mid-install.
_ZERO_CHUNK = bytes(_CHUNK)
_ZERO_MV = memoryview(_ZERO_CHUNK)
_FF_CHUNK = b"\xff" * _CHUNK
_FF_MV = memoryview(_FF_CHUNK)

# Bytes of image written between PROACTIVE, relax()-fed garbage collections during the write.
# Preallocation removed the megabyte-scale delta churn, but small residual churn (memoryview
# slices, the compressed-download reader) still trips an automatic GC every few MB -- and on the
# N6's multi-MB heap ANY collection is ~65-100 ms (the pause is heap-size-bound, not garbage-bound),
# which under a watchdog can outrun the window. So when a watchdog is armed we collect PROACTIVELY
# on this cadence, keeping free heap high enough that automatic GC never fires mid-write; each
# proactive collect runs under relax() (ISR-fed), so its pause can't bite. GC is never disabled.
_GC_EVERY = 512 * _CHUNK      # ~2 MB: well under the ~5 MB automatic-GC interval measured on HW

# Socket timeout (s) for the download: bounds the TLS handshake and every recv so a
# stalled connection fails the install cleanly (-> reboot to golden) instead of hanging.
_SOCK_TIMEOUT = 30
# Poll slice (ms) for the non-blocking download recv: the reader waits for data in slices this long,
# feeding the watchdog each slice, so it must stay WELL under the watchdog window (~100 ms) -> a few
# feeds per window. Only matters when a watchdog is armed; a fast recv returns the moment data arrives.
_RECV_POLL_MS = 20
# "Operation would block" errnos a non-blocking recv may raise (EAGAIN/EWOULDBLOCK) even right after
# poll reports readable -- a TLS socket can be TCP-readable with no whole record decrypted yet. Both
# the positive (11) and MicroPython's negated (-11) form; treated as "no data yet", keep polling.
_EAGAIN = (11, -11)
_ETIMEDOUT = 110       # errno for a dead-link recv timeout. A NUMERIC errno is what marks a pre-erase
#                        failure as TRANSPORT (transient -> retry) vs an update REJECTION (raised with
#                        a descriptive string) -- see _is_transport_error + the run() manifest-fetch except.


class _TransportError(OSError):
    """A failure while ESTABLISHING or READING the connection -- as opposed to a verdict on the
    update itself. Raised (wrapping the cause) around the pre-erase manifest fetch, so run() can
    tell the two apart WITHOUT type/errno sniffing: WHERE it failed is the honest discriminator,
    not what the failing layer happened to raise. Both a socket error and a TLS error surface as
    ``OSError`` with a numeric code, and both a corrupt manifest and a cert-validity failure
    surface as ``ValueError`` -- so no amount of inspecting the exception can separate them."""


def _is_transport_error(e):
    """True if ``e`` came from the connection phase (DNS, TCP, the TLS handshake including cert
    validation, or reading the body) rather than from vetting the fetched manifest. run() uses this
    to DEFER+retry instead of logging install.reject, so neither a flaky link nor a not-yet-synced
    clock reads as "this release was refused" -- while a bad signature/key, failed anti-rollback
    vetting, or a corrupt manifest still does."""
    return isinstance(e, _TransportError)


# --- pure: URL + HTTP (host-testable) ---------------------------------------

def _resolve_url(manifest_url, rep_url):
    """Resolve a manifest representation URL. An ``https://`` URL is used as-is (an
    off-host CDN); otherwise it's relative to the manifest's own URL (the common case --
    artifacts published beside the manifest), so a signed manifest stays valid wherever
    it's hosted."""
    if rep_url.startswith("https://"):
        return rep_url
    if rep_url.startswith("./"):
        rep_url = rep_url[2:]
    return manifest_url.rsplit("/", 1)[0] + "/" + rep_url


def _parse_url(url):
    """Split an ``https://host[:port]/path`` URL into ``(host, port, path)``. Raises
    ValueError for anything but https -- the installer never speaks plaintext HTTP."""
    if not url.startswith("https://"):
        raise ValueError("install URL must be https:// (got %r)" % url)
    rest = url[8:]
    slash = rest.find("/")
    if slash < 0:
        hostport, path = rest, "/"
    else:
        hostport, path = rest[:slash], rest[slash:]
    if ":" in hostport:
        host, _, port_s = hostport.partition(":")
        try:
            port = int(port_s)
        except ValueError:
            raise ValueError("bad port in URL: %r" % url)
    else:
        host, port = hostport, 443
    if not host:
        raise ValueError("no host in URL: %r" % url)
    return host, port, path


def _request_bytes(host, port, path, start=0):
    """The HTTP/1.1 GET request line + headers. ``Connection: close`` so the server
    ends the body by closing -- and the gzip stream is self-terminating regardless.

    ``start`` > 0 adds ``Range: bytes=START-`` to RESUME an interrupted download at the
    compressed byte offset already consumed (see _ResumingBody)."""
    hosthdr = host if port == 443 else "%s:%d" % (host, port)
    rng = "Range: bytes=%d-\r\n" % start if start else ""
    return ("GET %s HTTP/1.1\r\nHost: %s\r\n"
            "User-Agent: openmv-ota\r\nAccept: */*\r\n%sConnection: close\r\n\r\n"
            % (path, hosthdr, rng)).encode()


def _parse_status(line):
    """The numeric status from a ``b'HTTP/1.1 200 OK'`` line; ValueError if malformed."""
    parts = line.split(None, 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
        raise ValueError("bad status line: %r" % line)
    try:
        return int(parts[1])
    except ValueError:
        raise ValueError("bad status code: %r" % line)


def _is_redirect(code):
    return code in (301, 302, 303, 307, 308)


def _chunk_size(line):
    """The size from a chunked-encoding size line (hex, optional ``;ext``)."""
    semi = line.find(b";")
    if semi >= 0:
        line = line[:semi]
    line = line.strip()
    if not line:
        raise ValueError("empty chunk size")
    return int(line, 16)


class _Reader:
    """A small buffered reader over a ``recv(n) -> bytes`` callable (``b''`` == EOF):
    line reads for the status/headers/chunk-sizes, plus bounded raw reads for the
    body. Holds any bytes read past the headers so the body stream sees them."""

    def __init__(self, recv, feed=_noop, buf=b""):
        self._recv = recv
        self._feed = feed          # progress-fed download: fed while WAITING for the next recv
        self._buf = buf

    def _fill(self):
        # MAIN-THREAD progress-fed recv. The socket is NON-BLOCKING (set by _open), so recv returns at
        # once: data, or None/EAGAIN when nothing has arrived yet -- on which we feed() and sleep one
        # short slice before retrying. This keeps a fixed feed cadence with NO reliance on select.poll()
        # or a timer ISR, which is the crux for the AE3: its SSL-socket poll() blocks THROUGH its own
        # timeout (starving a poll-gated feed), and relax()'s timer ISR is not serviced inside an mbedtls
        # read -- only a feed driven from the main thread between recvs is reliable there. Reading
        # WHATEVER IS AVAILABLE (< _CHUNK is fine; the caller re-fills) also avoids a *blocking* read that
        # waits for a whole chunk -- which mid-stream spans several TLS records on a paced Wi-Fi link and
        # starves the watchdog between reads (that bit the WWDG on the N6 image download). A dead link
        # produces nothing until _SOCK_TIMEOUT -> clean install error -> golden. Feed is a no-op unless a
        # watchdog is armed; the watchdog-off path is unaffected (recv still returns data the same way).
        waited = 0
        while True:
            self._feed()
            try:
                d = self._recv(_CHUNK)               # non-blocking: available bytes / None / b'' (EOF)
            except OSError as e:                      # would-block -> treat as "no data yet"
                if not (e.args and e.args[0] in _EAGAIN):
                    raise                             # a real error (ECONNRESET, ...) -> install -> golden
                d = None
            if d:
                self._buf += d
                return True
            if d is not None:                        # d == b'' -> EOF
                return False
            sleep_ms(_RECV_POLL_MS)                  # d is None -> no data yet; feed + wait a slice
            waited += _RECV_POLL_MS
            if waited >= _SOCK_TIMEOUT * 1000:
                raise OSError(_ETIMEDOUT, "recv timed out")  # dead link: NUMERIC errno -> transport (retry)

    def readline(self, limit=8192):
        while b"\n" not in self._buf:
            if len(self._buf) >= limit:
                raise ValueError("HTTP line too long")
            if not self._fill():
                break
        nl = self._buf.find(b"\n")
        if nl < 0:
            line, self._buf = self._buf, b""
            return line
        line, self._buf = self._buf[:nl + 1], self._buf[nl + 1:]
        return line

    def read_exact(self, n):
        while len(self._buf) < n:
            if not self._fill():
                raise ValueError("unexpected EOF")
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def read_some(self, n):
        """Up to ``n`` bytes (one buffer's worth); ``b''`` at EOF."""
        if not self._buf and not self._fill():
            return b""
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


def _read_response(reader):
    """Read the status line + headers from ``reader``; return ``(code, headers)`` with
    header names lowercased. Leaves ``reader`` positioned at the body."""
    code = _parse_status(reader.readline())
    headers = {}
    while True:
        line = reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        i = line.find(b":")
        if i >= 0:
            headers[line[:i].strip().lower()] = line[i + 1:].strip()
    return code, headers


class _Body(io.IOBase):
    """The response body as a readable stream, de-framing ``Transfer-Encoding:
    chunked`` or honouring ``Content-Length`` (or reading to EOF when neither is
    given -- a ``Connection: close`` body). Subclasses ``io.IOBase`` so MicroPython's
    ``deflate.DeflateIO`` can consume it; ``readinto`` is the only stream method
    needed for a read-only download."""

    def __init__(self, reader, length, chunked):
        self._r = reader
        self._left = length       # Content-Length remaining, or None for read-to-EOF
        self._chunked = chunked
        self._chunk_left = 0
        self._eof = False

    def _read(self, n):
        if self._eof:
            return b""
        if self._chunked:
            if self._chunk_left == 0:
                size = _chunk_size(self._r.readline())
                if size == 0:                       # last chunk: skip trailers
                    while self._r.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    self._eof = True
                    return b""
                self._chunk_left = size
            data = self._r.read_some(n if n < self._chunk_left else self._chunk_left)
            if not data:
                raise ValueError("unexpected EOF in chunk")
            self._chunk_left -= len(data)
            if self._chunk_left == 0:
                self._r.read_exact(2)               # the CRLF after the chunk data
            return data
        if self._left is None:                      # read to EOF
            data = self._r.read_some(n)
            if not data:
                self._eof = True
            return data
        if self._left <= 0:
            self._eof = True
            return b""
        data = self._r.read_some(n if n < self._left else self._left)
        if not data:
            raise ValueError("unexpected EOF in body")
        self._left -= len(data)
        return data

    def readinto(self, buf):
        data = self._read(len(buf))
        buf[:len(data)] = data
        return len(data)


def _make_body(reader, headers):
    """Build the body stream from the response headers (chunked / Content-Length /
    close-delimited)."""
    te = headers.get(b"transfer-encoding", b"")
    if b"chunked" in te.lower():
        return _Body(reader, None, True)
    cl = headers.get(b"content-length")
    if cl is not None:
        try:
            length = int(cl)
        except ValueError:
            raise ValueError("bad Content-Length: %r" % cl)
        return _Body(reader, length, False)
    return _Body(reader, None, False)


def _read_all(body, limit):
    """Read a whole (small) response body into bytes, capped at ``limit`` -- for the
    manifest, which is fetched into RAM rather than streamed to flash. Raises if it
    exceeds ``limit`` (a runaway/oversized manifest)."""
    parts, total = [], 0
    buf = bytearray(512)
    while True:
        n = body.readinto(buf)
        if not n:
            return b"".join(parts)
        total += n
        if total > limit:
            raise ValueError("manifest larger than %d bytes" % limit)
        parts.append(bytes(buf[:n]))                 # collect + join once, not o(n^2) +=


# --- pure: signed manifest (kept in sync with openmv_ota.ota.manifest) ------------
# The installer parses + selects from the manifest here (pre-erase, /rom intact); run()
# verifies the signature with the frozen ecdsa_verify C module + cfg.TRUSTED_KEYS, exactly
# as boot.py verifies an image trailer.

_MANIFEST_MAGIC = b"OMVM"
_MANIFEST_HEADER_VERSION = 1
_MANIFEST_SCHEMA = 1
_MANIFEST_HEADER_STRUCT = "<4sIIIIi"          # magic, hver, body_size, sig_size, key_id, alg
_MANIFEST_HEADER_SIZE = struct.calcsize(_MANIFEST_HEADER_STRUCT)   # 24
_MANIFEST_MAX = 8192
# COSE alg id -> raw R||S signature length (mirror of openmv_ota.ota.algorithms / boot.py).
_ALG_SIG_SIZE = {-7: 64, -35: 96, -36: 132}
# Image trailer header (mirror of openmv_ota.ota.trailer) -- only payload_version is read.
_TRAILER_MAGIC = b"OMVR"
_TRAILER_HEADER_STRUCT = "<4sIIIIIIIIIIi32s"


def _manifest_parse(data):
    """Structurally parse + CRC-check a manifest, returning a dict with the signed
    ``body``, the ``key_id``/``sig_alg`` (to pick the key), the ``signature``, and the
    exact ``region`` the signature covers. Raises ValueError on any malformation -- the
    signature itself is checked by the caller against the trusted keys."""
    if len(data) < _MANIFEST_HEADER_SIZE:
        raise ValueError("manifest too small")
    magic, hver, body_size, sig_size, key_id, sig_alg = struct.unpack_from(
        _MANIFEST_HEADER_STRUCT, data, 0)
    if magic != _MANIFEST_MAGIC:
        raise ValueError("bad manifest magic")
    if hver != _MANIFEST_HEADER_VERSION:
        raise ValueError("bad manifest header_version")
    expect_sig = _ALG_SIG_SIZE.get(sig_alg)
    if expect_sig is None or sig_size != expect_sig:
        raise ValueError("bad manifest alg/sig_size")
    body_end = _MANIFEST_HEADER_SIZE + body_size + sig_size
    if body_end + 4 > len(data):
        raise ValueError("manifest truncated")
    crc = struct.unpack_from("<I", data, body_end)[0]
    if (binascii.crc32(data[:body_end]) & 0xFFFFFFFF) != crc:
        raise ValueError("manifest crc mismatch")
    region = bytes(data[:_MANIFEST_HEADER_SIZE + body_size])
    body = json.loads(data[_MANIFEST_HEADER_SIZE:_MANIFEST_HEADER_SIZE + body_size])
    signature = bytes(data[_MANIFEST_HEADER_SIZE + body_size:body_end])
    return {"body": body, "key_id": key_id, "sig_alg": sig_alg,
            "signature": signature, "region": region}


def _update_reject(body, product_id, platform_version, rollback_floor, account_id=""):
    """Device-relative pre-flight check on a verified manifest body -- the mirror of
    boot.evaluate_slot's image checks (and openmv_ota.ota.manifest.update_reject_reason).
    Returns a reason string to reject, or None to proceed."""
    if body.get("schema") != _MANIFEST_SCHEMA:
        return "schema"
    if product_id and body.get("product_id", 0) != product_id:
        return "board"
    if account_id and body.get("account_id", "") != account_id:
        return "account"
    mpv = body.get("min_platform_version", 0)
    if mpv and mpv > platform_version:
        return "compat"
    if body.get("payload_version", 0) < rollback_floor:
        return "rollback"
    return None


def _select_rep(body, delta_capable, golden_payload_version):
    """Pick the cheapest usable representation (mirror of
    openmv_ota.ota.manifest.select_representation). Returns the rep dict, or None."""
    best = None
    for rep in body.get("representations", []):
        fmt = rep.get("format")
        if fmt == _DELTA_FORMAT:
            if not delta_capable or rep.get("base_payload_version") != golden_payload_version:
                continue
        elif fmt != "full":
            continue
        if best is None or rep.get("size", 1 << 62) < best.get("size", 1 << 62):
            best = rep
    return best


def _golden_floor(trailer):
    """The anti-rollback floor: BACK golden's ``payload_version`` (mirror of
    boot._rollback_floor). 0 if BACK's trailer doesn't parse (a torn factory image)."""
    if len(trailer) < struct.calcsize(_TRAILER_HEADER_STRUCT):
        return 0
    fields = struct.unpack_from(_TRAILER_HEADER_STRUCT, trailer, 0)
    if fields[0] != _TRAILER_MAGIC:
        return 0
    return fields[8]                              # payload_version (9th header field)


# --- pure: delta apply (kept in sync with openmv_ota.ota.delta) --------------
# A selected delta is reconstructed against the golden BACK slot: for each op, emit the
# `extra` literals, seek the base cursor, then emit the diff region = BACK + diff (mod 256).
# The diff stream is image-sized (mostly zeros), so the patch is *streamed* through the
# decompressor (never held whole in RAM). The add is vectorised with ulab on-device (with a
# pure fallback); the result is still sha256- + trailer-verified, so the patch isn't trusted.

_DELTA_FORMAT = "ocdl"                            # manifest representation["format"]
_DELTA_MAGIC = b"OCDL"

try:                                              # ulab numpy: on every OTA-capable board
    from ulab import numpy as _np
except ImportError:                               # host / a board without ulab -> pure add
    _np = None


def _add(old_b, diff_b):
    """``(old_b + diff_b) mod 256`` for the diff region. All-zero diff (the unchanged bulk)
    is a straight copy; otherwise ulab vectorises the add, with a pure-Python fallback."""
    n = len(diff_b)
    if diff_b == (_ZERO_CHUNK if n == _CHUNK else bytes(n)):   # unchanged bulk -> straight copy
        return bytes(old_b)
    if _np is not None:
        return (_np.frombuffer(old_b, dtype=_np.uint8)        # pragma: no cover (device/ulab)  # hil-residual: ulab vectorised add (device-only, no ulab on host); correctness proven end-to-end by the delta scenario's sha256 gate (install.armed -> confirm.promoted); the pure-Python twin below is host-tested
                + _np.frombuffer(diff_b, dtype=_np.uint8)).tobytes()
    return bytes((old_b[i] + diff_b[i]) & 0xFF for i in range(len(diff_b)))


class _PatchReader:
    """Zero-alloc buffered reader over a streamed patch source -- a ``DeflateIO`` (``readinto``,
    preferred) or a plain ``read(n)`` stub. Fills ONE reused buffer and serves exact reads (into a
    caller memoryview) + varints by index, so the image-sized decompressed diff stream is consumed
    in a single forward pass with no per-read allocation (no GC churn to bite an armed watchdog)."""

    def __init__(self, src):
        self._src = src
        self._readinto = getattr(src, "readinto", None)   # DeflateIO has it; the read()-only stub doesn't
        self._buf = bytearray(_CHUNK)
        self._mv = memoryview(self._buf)
        self._len = 0                                     # valid bytes currently in _buf
        self._off = 0                                     # consumed offset within _buf

    def _more(self):
        """Refill the (fully consumed) buffer from src; False at EOF."""
        if self._readinto is not None:
            self._len = self._readinto(self._buf)
        else:
            d = self._src.read(_CHUNK)                     # stub path: read() -> copy in
            self._len = len(d)
            self._buf[:self._len] = d
        self._off = 0
        return self._len > 0

    def read_into(self, dst):
        """Fill ``dst`` (a memoryview) with exactly ``len(dst)`` patch bytes; raise on a short patch."""
        m = len(dst)
        got = 0
        while got < m:
            if self._off >= self._len and not self._more():
                raise OSError("delta truncated")
            take = self._len - self._off
            if take > m - got:
                take = m - got
            dst[got:got + take] = self._mv[self._off:self._off + take]
            self._off += take
            got += take

    def read_exact(self, k):
        """``k`` bytes as ``bytes`` -- only the 4-byte magic + tests; the hot path uses read_into."""
        out = bytearray(k)
        self.read_into(memoryview(out))
        return bytes(out)

    def read_uvarint(self):
        result = shift = 0
        while True:
            if self._off >= self._len and not self._more():
                raise OSError("delta truncated")
            b = self._buf[self._off]
            self._off += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result
            shift += 7

    def read_svarint(self):
        zz = self.read_uvarint()
        return (zz >> 1) if not (zz & 1) else -((zz + 1) >> 1)


def _delta_stream(reader, old_read, chunk):
    """Yield the reconstructed image in <=``chunk`` pieces from a streamed OCDL patch (``reader``)
    + the golden base (``old_read(off, n)`` -> a view over the XIP'd BACK slot). Mirror of
    openmv_ota.ota.delta.apply_delta, streamed both ways so neither is held whole. Each piece is a
    memoryview into ONE reused buffer -- the consumer MUST copy it before pulling the next (that is
    what ``_GenReader`` does). Zero per-piece allocation: the diff add is done in place (ulab on
    device; a pure-Python twin off it). Raises OSError on a bad/short patch (-> reboot to golden)."""
    if reader.read_exact(4) != _DELTA_MAGIC:
        raise OSError("bad delta magic")
    target_size = reader.read_uvarint()
    out = bytearray(chunk)                                # reused output buffer for every yielded piece
    mv = memoryview(out)
    acc = _np.frombuffer(out, dtype=_np.uint8) if _np is not None else None   # writable view over out
    old = produced = 0
    while produced < target_size:
        extra_len = reader.read_uvarint()
        diff_len = reader.read_uvarint()
        old += reader.read_svarint()
        left = extra_len
        while left:                                      # literal run: patch bytes verbatim
            m = left if left < chunk else chunk
            reader.read_into(mv[:m])
            yield mv[:m]
            produced += m
            left -= m
        o = old
        left = diff_len
        while left:                                      # diff run: (BACK + patch) mod 256
            m = left if left < chunk else chunk
            reader.read_into(mv[:m])                      # out[:m] = the diff bytes
            old_v = old_read(o, m)                        # BACK view (XIP alias, no copy)
            if mv[:m] == _ZERO_MV[:m]:                    # unchanged bulk -> result is just BACK
                mv[:m] = old_v
            elif acc is not None:                         # ulab in-place: out += BACK (uint8 wraps)
                acc[:m] += _np.frombuffer(old_v, dtype=_np.uint8)  # pragma: no cover (device/ulab)  # hil-residual: ulab in-place vectorised add (device-only, no ulab on host); the pure branch below is host-tested and the delta scenario's sha256 gate proves the ulab path end-to-end (install.armed -> confirm.promoted)
            else:                                         # pure fallback (host + no-ulab boards)
                for i in range(m):
                    out[i] = (out[i] + old_v[i]) & 0xFF
            yield mv[:m]
            o += m
            left -= m
        old += diff_len
        produced += diff_len


class _GenReader:
    """Adapt a generator of memoryview pieces to a ``readinto(dst)`` source for _install_stream:
    copy each piece into the caller's buffer (overflow into one reused hold buffer) so the write
    loop stays zero-alloc. ``feed`` fires per generator piece -- one _CHUNK fill can pull MANY delta
    pieces (each a patch read + BACK read + add) while the write loop only feeds once per _CHUNK, so
    feeding here keeps an armed watchdog alive between those coarser feeds. No-op feed by default."""

    def __init__(self, gen, feed=_noop):
        self._gen = gen
        self._feed = feed
        self._hold = bytearray(_CHUNK)                    # a partial piece carried across readinto()s
        self._hmv = memoryview(self._hold)
        self._hlen = 0                                    # valid bytes in _hold
        self._hoff = 0                                    # consumed offset in _hold

    def readinto(self, dst):
        """Fill ``dst`` (a memoryview) with up to ``len(dst)`` reconstructed bytes; 0 at EOF."""
        need = len(dst)
        filled = 0
        while filled < need:
            if self._hoff < self._hlen:                  # drain the carried-over remainder first
                take = self._hlen - self._hoff
                if take > need - filled:
                    take = need - filled
                dst[filled:filled + take] = self._hmv[self._hoff:self._hoff + take]
                self._hoff += take
                filled += take
                continue
            try:
                piece = next(self._gen)                   # a memoryview into the delta's reused buffer
                self._feed()
            except StopIteration:
                break
            plen = len(piece)
            take = plen if plen <= need - filled else need - filled
            dst[filled:filled + take] = piece[:take]
            filled += take
            if take < plen:                               # stash the leftover before the buffer is reused
                rem = plen - take
                self._hmv[:rem] = piece[take:]
                self._hlen = rem
                self._hoff = 0
        return filled


# --- pure: the flash write (host-testable; all I/O injected) -----------------

def _is_blank(chunk):
    """True if ``chunk`` is all 0xFF -- already-erased flash we needn't rewrite."""
    return chunk == _FF_MV[:len(chunk)]          # compare vs a hoisted view -> no per-call alloc


class _Progress:
    """Per-chunk install progress -> the (frozen) logger, throttled to one line per new
    10% step. Defined *here* so ``exec`` compiles it into RAM: it is called from the write
    loop *after* the FRONT slot is erased, so its bytecode must not live in that slot. A
    reporter (or any app callback) from the romfs ``openmv_ota``/app -- which is in the
    slot being erased -- would XIP its bytecode from erased flash and fault. For the same
    reason install progress is log-only: there is no safe app callback to invoke here."""

    def __init__(self, log):
        self._log = log
        self._step = -1

    def reset(self):
        self._step = -1                            # restart the 10% steps for a retried download

    def __call__(self, done, total):
        pct = done * 100 // total if total else 100
        step = pct // 10
        if step > self._step:
            self._step = step
            self._log.info("install: %d%% (%d/%d bytes)" % (pct, done, total))


def _install_stream(source, write, readback, front_size, block, feed,
                    progress=None, expect_sha=None, repr_marker=None, gc_collect=None):
    """Stream the decompressed image into the ALREADY-ERASED FRONT slot 1:1
    (verifying every write by read-back, skipping already-erased 0xFF runs), then
    arm the trial.

    The caller MUST erase the FRONT slot BEFORE calling this AND before opening the
    download stream ``source`` draws from -- so the download socket is never left idle
    during the multi-second erase (a slow flash on a power-saving link drops an idle
    connection, and the write loop would then read a truncated body). This function
    starts by read-back verifying the slot is fully erased.

    ``source.readinto(mv)`` fills up to ``len(mv)`` decompressed image bytes into the caller's
    buffer and returns the count (0 at EOF) -- a ``DeflateIO`` for a full image, or a ``_GenReader``
    over the delta stream; either way the write loop reuses ONE ``_CHUNK`` buffer, so no per-chunk
    allocation happens and automatic GC never fires to bite an armed watchdog. ``write(off, data)``
    programs flash; ``readback(off, n)`` returns the ``n`` bytes at ``off``; ``feed()`` is called
    once per chunk so the watchdog stays alive through the loops *without* masking a hang (if the
    loop stops iterating, feeding stops); ``progress(done, front_size)`` (if given) is called once
    per written chunk; ``expect_sha`` (if given, the manifest's hex sha256 of the reconstructed
    image) is checked over the streamed bytes and must match; ``repr_marker`` (if given) records
    which representation was applied (REPR_FULL / REPR_DELTA) for status() to report. Raises on any
    size/hash mismatch or read-back miscompare; this runs after the erase, so the caller turns any
    exception into a reboot into golden."""
    off = 0
    while off < front_size:                          # confirm the caller's erase took
        n = _CHUNK if front_size - off >= _CHUNK else front_size - off
        if not _is_blank(readback(off, n)):
            raise OSError("erase verify failed at %d" % off)
        off += n
        feed()

    digest = hashlib.sha256() if expect_sha is not None else None
    work = bytearray(_CHUNK)                          # the ONE reused write buffer -> zero-alloc loop
    mv = memoryview(work)
    off = 0
    since_gc = 0
    while off < front_size:
        feed()                                       # before the (recv + delta reconstruct) fill
        want = _CHUNK if front_size - off >= _CHUNK else front_size - off
        n = 0
        while n < want:                              # fill a full aligned chunk: re-chunks the
            k = source.readinto(mv[n:want])          # arbitrary delta/deflate pieces into one buffer
            if k == 0:
                break                                # EOF: a short tail is caught by the size check
            n += k
        if n == 0:
            break                                    # source exhausted; the size check below rejects short
        chunk = mv[:n]
        if digest is not None:
            digest.update(chunk)
        if not _is_blank(chunk):                     # erased regions are already 0xFF
            write(off, chunk)
            if readback(off, n) != chunk:
                raise OSError("write verify failed at %d" % off)
        off += n
        feed()
        since_gc += n
        if gc_collect is not None and since_gc >= _GC_EVERY:
            gc_collect()                             # proactive relax()-fed collect -> auto-GC never fires
            since_gc = 0
        if progress is not None:
            progress(off, front_size)
    if off == front_size and source.readinto(mv[:1]):    # any bytes beyond the slot -> wrong image
        raise ValueError("image larger than the %d-byte slot" % front_size)
    if off != front_size:
        raise ValueError("image is %d bytes, expected a full %d-byte slot"
                         % (off, front_size))
    # MicroPython's hashlib has no .hexdigest() (CPython-only) -- hexlify the raw
    # digest instead, so the check runs identically on-device and on the host.
    if digest is not None and binascii.hexlify(digest.digest()).decode() != expect_sha:
        log.warning("install: reject sha")           # the integrity trust boundary rejected the image
        raise OSError("image sha256 does not match the manifest")

    pending_off = front_size - 2 * block             # the status sector
    if repr_marker is not None:                      # record which rep was applied (1->0 only)
        write(pending_off + _REPR_OFF, repr_marker)
        if readback(pending_off + _REPR_OFF, len(repr_marker)) != repr_marker:
            raise OSError("repr marker verify failed")
    write(pending_off, PENDING)                       # arm the one-shot trial, LAST
    if readback(pending_off, len(PENDING)) != PENDING:
        raise OSError("arm verify failed")


# --- device entry (wires real socket/ssl/deflate/vfs/machine) ---------------
# Excluded from host coverage like boot.py's _main; exercised under QEMU only for the
# exec-into-RAM + clean-failure path (qemu has no network and a read-only rom_ioctl).

def _connect(host, port, ca_pem, socket, ssl):  # pragma: no cover
    """A TLS socket to ``host:port`` verified against ``ca_pem`` (CERT_REQUIRED + SNI).
    mbedtls copies the cert at load, and the handshake completes here -- both before
    any erase -- so ``ca_pem`` (read from the about-to-be-erased romfs) is safe."""
    ai = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0]
    sock = socket.socket(ai[0], ai[1], ai[2])
    try:
        sock.settimeout(_SOCK_TIMEOUT)               # so a stalled handshake/recv can't block forever
        # connect + TLS handshake are blocking, unsplittable mbedtls C ops that can exceed a short
        # watchdog window; relax() ISR-feeds across them (no-op unless the app armed a watchdog).
        with (openmv_wdt.relax() if openmv_wdt is not None else _NoWdt()):
            sock.connect(ai[-1])
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cadata=ca_pem)
            tls = ctx.wrap_socket(sock, server_hostname=host)
        log.debug("install: TLS up")
        return tls  # hil-residual: bare return of the wrapped TLS socket
    except Exception:  # hil-residual: connect-failure cleanup wrapper
        sock.close()  # hil-residual: bare cleanup close on a failed connect (error path)
        raise  # hil-residual: bare re-raise to the caller (pre-erase, /rom intact)


def _open(url, ca_pem, socket, ssl, feed=_noop, max_redirects=5, start=0):  # pragma: no cover
    """Connect, GET, follow redirects, and return ``(sock, body)`` on a 2xx -- all
    before the erase, so a bad URL / DNS / TLS / non-2xx status raises to the app
    with /rom still intact. ``feed`` lets the body reader keep an armed watchdog fed
    between recvs (progress-fed, non-blocking) without a relax()."""
    for _ in range(max_redirects + 1):
        host, port, path = _parse_url(url)
        sock = _connect(host, port, ca_pem, socket, ssl)
        try:
            sock.write(_request_bytes(host, port, path, start))  # blocking send of the small GET
            # Go NON-BLOCKING for every read from here (headers AND body): the reader then feeds the
            # watchdog from the main thread between recvs (see _Reader._fill). This is what a slow FIRST
            # byte needs -- the server can be seconds slow to answer (a stored delta whose first fetch
            # misses the OS page cache), and on the AE3 neither poll() nor relax() feeds that wait
            # (its SSL poll() blocks through its timeout; relax()'s ISR isn't serviced inside an mbedtls
            # read). A main-thread non-blocking recv + sleep loop feeds it regardless. Bounded by
            # _SOCK_TIMEOUT (the reader's wait counter), so a dead server still falls cleanly to golden.
            sock.setblocking(False)
            reader = _Reader(sock.read, feed)
            code, headers = _read_response(reader)
        except Exception:
            sock.close()
            raise
        if _is_redirect(code):
            sock.close()
            loc = headers.get(b"location")
            if not loc:
                raise OSError("redirect (%d) with no Location" % code)
            url = loc.decode()
            continue
        if not (200 <= code < 300):
            sock.close()
            raise OSError("HTTP %d" % code)
        if start and code != 206:
            # We asked to RESUME but the server sent the whole entity from byte 0 (no Range
            # support, or a proxy that stripped it). We cannot use it: the decompressor is
            # mid-stream and its state cannot be rewound, so replaying from 0 would corrupt the
            # image. Fail cleanly -- the caller falls to golden and the next poll starts over.
            sock.close()
            raise OSError("resume unsupported: HTTP %d for a Range request" % code)
        log.debug("install: fetched body")
        return sock, _make_body(reader, headers)
    raise OSError("too many redirects")


_RESUME_MAX_STALLS = 10   # consecutive re-opens that deliver ZERO new bytes -> genuinely stuck


class _ResumingBody(io.IOBase):
    """A download body that survives the connection being dropped mid-transfer.

    Subclasses ``io.IOBase`` for the same reason ``_Body`` does: on MicroPython that is what makes
    a Python object usable as a C-LEVEL STREAM, which is what ``deflate.DeflateIO`` requires of its
    source. A plain class with a ``readinto`` method is NOT enough -- DeflateIO rejects it at the
    first read with ``OSError('stream operation not supported')``, which on the bench looked like a
    mid-install failure that fell back to golden. Host tests cannot catch this (CPython happily
    reads any object with the right methods), so it is pinned by an explicit subclass assertion.

    A poor link cannot always finish a long download in one connection: the WINC1500 aborts
    EVERY transfer at ~50 s regardless of how many bytes have moved, so a 4 MiB image never
    completed and the installer burned its 3 attempts re-erasing and restarting from zero.

    Restarting is the wrong response, because nothing is actually lost: the gzip/delta decoder
    lives in RAM and keeps its state across a dead socket. Only the PIPE needs replacing. So we
    count the COMPRESSED bytes handed to the decoder and, on a drop, re-open the same URL with
    ``Range: bytes=<consumed>-`` and keep feeding the SAME decoder. The byte stream stays
    continuous; only the transport underneath is segmented. That needs no format change and costs
    O(1) RAM -- a counter -- so it stays inside the module's RAM budget.

    Re-opens are UNBOUNDED WHILE MAKING PROGRESS: a slow-but-working link should finish, and an
    attempt cap would abandon it for being slow rather than for being broken. "Stuck" is the
    honest failure, so only ``_RESUME_MAX_STALLS`` consecutive re-opens that deliver ZERO new
    bytes give up. This cannot run forever: the server's capability token expires (ttl 3600 s),
    after which a resume gets a 404 and falls cleanly to golden.
    """

    def __init__(self, url, ca_pem, socket, ssl, feed, sock, body):
        self._url, self._ca = url, ca_pem
        self._socket, self._ssl, self._feed = socket, ssl, feed
        self._sock, self._body = sock, body
        self._pos = 0            # compressed bytes already delivered to the decoder
        self._stalls = 0

    def readinto(self, buf):
        while True:
            try:
                n = self._body.readinto(buf)
            except Exception as e:                    # socket drop, or a short/truncated body
                self._reopen(e)                       # raises once genuinely stuck
                continue
            self._pos += n
            if n:
                self._stalls = 0                      # forward progress -> the budget resets
            return n

    def _reopen(self, err):
        """Replace the dead connection with a Range request at the current offset."""
        self._stalls += 1
        if self._stalls > _RESUME_MAX_STALLS:         # no new bytes across N re-opens -> stuck
            raise err
        try:
            self._sock.close()
        except Exception:
            pass                                      # already dead; nothing else holds the fd
        self._feed()                                  # a re-connect is unsplittable -> feed across it
        log.info("install: resuming at %d (%r)" % (self._pos, err))   # HIL witness + field diagnostic
        self._sock, self._body = _open(self._url, self._ca, self._socket, self._ssl,
                                       self._feed, start=self._pos)

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


def _fetch_manifest(manifest_url, ca_pem, cfg, verify, socket, ssl, feed=_noop):  # pragma: no cover
    """Pre-erase: fetch the signed manifest, verify its signature against the frozen
    trusted keys (exactly as boot.py verifies an image trailer), apply the device-relative
    checks (board / platform / anti-rollback), and pick a representation. Returns
    ``(image_url, fmt, expect_sha)``. Raises (to the app, /rom intact) on any failure --
    nothing is erased."""
    import uctypes
    import vfs

    # CONNECTION PHASE -- open + read. Everything here (DNS, TCP, the TLS handshake and its cert
    # validation, each body read) is TRANSPORT: it says nothing about the release, so run() defers
    # and retries rather than reporting a rejection. Cert failures belong here too, and that matters:
    # a cold-booted board whose clock has not yet NTP-synced fails the handshake with "certificate
    # validity starts in the future" -- a state it RECOVERS from a poll later, never a bad update.
    # Wrapping by PHASE (not by exception type) is what makes that distinction reliable.
    try:
        sock, body = _open(manifest_url, ca_pem, socket, ssl, feed)
        try:
            raw = _read_all(body, _MANIFEST_MAX)      # progress-fed reader (poll+feed)
        finally:
            sock.close()
    except Exception as e:  # hil-residual: connection-phase failure wrapper (needs a dropped link / bad cert to reach; the happy path fetches cleanly)
        raise _TransportError("manifest fetch failed: %r" % (e,))  # hil-residual: bare raise (cause kept in the message; run() logs it as deferred + retries)
    # The manifest is now in RAM; everything below -- CRC parse, the ECDSA signature verify,
    # and the flash-vetting -- is pure CPU: single unsplittable C/Python calls with no seam to
    # feed through, and the P-256 verify alone can outrun the ~100 ms watchdog window (this is
    # the op that bit on the N6 watchdog HIL run). So if the app armed a watchdog, ONE relax()
    # ISR-feeds across the whole bounded block (last resort, exactly like the TLS handshake and
    # the exec compile). There is no network in here, so relax() cannot mask a stalled recv --
    # only the leaf reads above are progress-fed. No-op unless a watchdog is armed.
    with (openmv_wdt.relax() if openmv_wdt is not None else _NoWdt()):
        m = _manifest_parse(raw)                          # structure + crc (raises on bad)
        pubkey = cfg.TRUSTED_KEYS.get(m["key_id"])
        if pubkey is None:
            log.warning("install: reject untrusted key")
            raise OSError("manifest signed by an untrusted key")  # hil-residual: bare raise (reject witnessed by install.reject_key)
        if not verify(m["sig_alg"], pubkey, m["signature"], m["region"]):
            log.warning("install: reject bad signature")
            raise OSError("manifest signature does not verify")  # hil-residual: bare raise (reject witnessed by install.reject_sig)

        body_dict = m["body"]
        base = uctypes.addressof(vfs.rom_ioctl(2, 0))     # partition XIP base
        floor = _golden_floor(uctypes.bytearray_at(base + cfg.PARTITION_SIZE - cfg.OTA_BLOCK,
                                                  cfg.OTA_BLOCK))
        reason = _update_reject(body_dict, cfg.PRODUCT_ID, cfg.PLATFORM_VERSION, floor,
                                getattr(cfg, "ACCOUNT_ID", ""))
        if reason is not None:
            log.warning("install: reject vetting")   # rejected: witness the boundary
            raise OSError("manifest rejected (%s)" % reason)  # hil-residual: bare raise (reject witnessed by install.reject_vet)
        # The delta applier is pure Python (no ulab/C), so every board is delta-capable; the
        # delta is used only when its base matches this device's golden (BACK) version.
        rep = _select_rep(body_dict, True, floor)
    if rep is None:
        raise OSError("manifest has no usable representation")  # hil-residual: bare raise (no-rep guard, inject-only)
    image_url = _resolve_url(manifest_url, rep["url"])
    fmt = rep.get("format")
    expect_sha = body_dict.get("sha256")
    log.debug("install: manifest accepted")
    return image_url, fmt, expect_sha  # hil-residual: bare return of the (url, fmt, sha) tuple


def _reset():  # pragma: no cover
    """Reboot into the freshly-selected slot, but let the frozen logger's handler drain
    first. ``machine.reset()`` cuts an in-flight UART TX, so the final line -- e.g.
    'installed + armed' -- is truncated mid-string and lost on a side-channel UART (the FIFO
    is only tens of bytes; most of the line is still buffered at reset). A short settle drains
    it. Harmless in production: a reboot is never time-critical, and it makes the last log line
    reliably land wherever the logger points (UART/socket/REPL)."""
    import time
    import machine
    log.debug("install: rebooting")                   # witnessed before the drain settle below
    time.sleep_ms(50)  # hil-residual: bare settle (drains the logger's UART FIFO before reset)
    machine.reset()  # hil-residual: terminal reset (no post-reset witness)


def run(manifest_url, ca_pem, cfg):  # pragma: no cover
    """Fetch the signed manifest at ``manifest_url``, verify + vet it, then download and
    install the chosen image. Never returns: reboots into the new image's trial on
    success, or into the golden BACK image if anything fails after the erase commits.
    A pre-flight failure (bad URL/DNS/TLS, bad/forbidden manifest) raises to the app with
    ``/rom`` intact. Progress is logged from here (RAM + the frozen logger) at every 10%
    step -- it can't be a caller callback, whose code is being erased."""
    import deflate
    import socket
    import ssl

    import uctypes
    import vfs
    from ecdsa_verify import verify                  # the frozen C module (as in boot.py)

    # Watchdog (if the app enabled one): relax() feeds it from a timer ISR ONLY around the
    # single multi-second erase the main loop can't reach; feed() keeps it alive per chunk
    # through the loops -- so a hung loop (or a stalled recv) still trips it -> golden.
    relax = openmv_wdt.relax if openmv_wdt is not None else _NoWdt
    feed = openmv_wdt.feed if openmv_wdt is not None else _noop
    # Proactive GC hook (only when a watchdog is armed): the write loop calls this on a byte
    # cadence to collect BEFORE the heap fills, so automatic GC never fires mid-write. gc.collect()
    # is a single unsplittable pause, so it runs under relax() (ISR-fed). GC is never disabled.
    gc_collect = None
    if openmv_wdt is not None:
        import gc as _gc  # hil-residual: watchdog-armed proactive collect (opt-in; covered by the watchdog HIL scenario)

        def gc_collect():  # hil-residual: watchdog-armed proactive collect (opt-in; covered by the watchdog HIL scenario)
            with relax():  # hil-residual: relax() feeds the WWDG across the unsplittable gc.collect()
                _gc.collect()  # hil-residual: proactive collection at a controlled point (device-only)
    # Log-only progress, built from RAM + the frozen logger so it survives the FRONT erase.
    progress = _Progress(log) if log is not None else None
    front_size, block = cfg.FRONT_SIZE, cfg.OTA_BLOCK

    # The romfs write path has two flavours across ports; detect which from the
    # FRONT partition object. An XIP-mapped port (stm32/alif/samd) returns a
    # buffer we address directly and erase/write via rom_ioctl(3/4/5). A
    # block-device port (mimxrt) returns a Flash object with the block protocol,
    # driven via ioctl(6)=erase-block + the extended (3-arg) writeblocks/readblocks
    # for byte-granular access. _install_stream is agnostic -- it only sees
    # erase/write/readback/back_read -- so all the divergence lives here.
    front = vfs.rom_ioctl(2, 0)
    _seen = set()                                     # one-shot log guard: emit each per-chunk
    if hasattr(front, "ioctl"):                       # write marker once (bounded, RAM-safe)
        log.debug("install: write path block-device")
        _bs = front.ioctl(5, 0)                       # block size
        # A block-device port exposes ONE segment covering the WHOLE partition, and
        # rom_ioctl(2, <id>) ignores the id (mimxrt returns the same object for 0 and 1).
        # So FRONT and BACK are the same device addressed by offset: FRONT at 0, BACK at
        # front_size -- exactly as the XIP branch does with base / base+front_size.

        def erase(total):
            nb = (total + _bs - 1) // _bs
            b = 0
            while b < nb:                             # one block per call -> returns to the
                front.ioctl(6, b)                     # VM between blocks (no dead-time erase);
                b += 1                                # this port is already chunk-granular
                feed()
                if log and "e" not in _seen:          # witness the in-loop erase op once
                    _seen.add("e")
                    log.debug("install: erasing block block-device")
            log.debug("install: erased FRONT block-device")

        # Reused block-device scratch (n <= _CHUNK): a readback + a BACK-read buffer, so neither
        # closure allocates per chunk -- the same zero-alloc discipline as the XIP path, needed so
        # an armed watchdog isn't bitten by GC churn on this port. Each returned view is consumed
        # (compared / added) before the next call reuses its buffer.
        _rb = memoryview(bytearray(_CHUNK))
        _br = memoryview(bytearray(_CHUNK))

        def write(off, data):                         # extended writeblocks: byte-granular,
            front.writeblocks(off // _bs, data, off % _bs)   # so sub-block markers work too
            if log and "w" not in _seen:
                _seen.add("w")
                log.debug("install: wrote block block-device")

        def readback(off, n):
            front.readblocks(off // _bs, _rb[:n], off % _bs)  # FRONT at partition offset off
            if log and "r" not in _seen:
                _seen.add("r")
                log.debug("install: readback block-device")
            return _rb[:n]  # hil-residual: bare return of the reused readback view

        def back_read(off, n):                        # arbitrary range from BACK, block-safe
            done = 0                                  # BACK lives at front_size within the one
            while done < n:                           # partition (NOT a separate rom_ioctl(2,1)
                a = front_size + off + done           # segment -- that returns FRONT on mimxrt)
                blk, o = a // _bs, a % _bs
                take = _bs - o
                if take > n - done:
                    take = n - done  # hil-residual: bare arithmetic clamp (final partial block)
                front.readblocks(blk, _br[done:done + take], o)
                done += take
                if log and "brl" not in _seen:        # witness the in-loop BACK read once
                    _seen.add("brl")
                    log.debug("install: back reading block-device")
            if log and "br" not in _seen:
                _seen.add("br")
                log.debug("install: back read block-device")
            return _br[:n]  # hil-residual: bare return of the reused BACK-read view

        def complete():
            log.debug("install: complete block-device")
        log.debug("install: write path ready block-device")

    else:                                             # XIP-mapped romfs (stm32/alif/samd)
        log.debug("install: write path XIP")
        base = uctypes.addressof(front)               # FRONT partition XIP base

        def readback(off, n):
            r = uctypes.bytearray_at(base + off, n)
            if log and "r" not in _seen:
                _seen.add("r")
                log.debug("install: readback XIP")
            return r  # hil-residual: bare return of the XIP readback alias

        def back_read(off, n):
            r = uctypes.bytearray_at(base + front_size + off, n)   # BACK at front_size
            if log and "br" not in _seen:
                _seen.add("br")
                log.debug("install: back read XIP")
            return r  # hil-residual: bare return of the XIP BACK-read alias

        def erase(total):
            # Erase INCREMENTALLY where the port supports the ranged prepare
            # (rom_ioctl 6 = min-prepare size, and the 4-arg rom_ioctl 3 with an
            # offset -- micropython PR #19348). One whole-slot erase is seconds of
            # dead time in a single C call: nothing services USB or the scheduler,
            # and on the N6 (12 MiB slot on XSPI) the device faults partway through.
            # Older firmware without the ranged form falls back to the legacy
            # single-shot erase under relax().
            bs = vfs.rom_ioctl(6, 0)
            if isinstance(bs, int) and bs > 0:
                o = 0
                while o < total:
                    n = bs if total - o > bs else total - o
                    rc = vfs.rom_ioctl(3, 0, o, n)
                    if rc < 0:
                        raise OSError(-rc)  # hil-residual: bare raise on a negative errno (erase-fault, inject-only)
                    o += n
                    feed()
                    if log and "e" not in _seen:      # witness the in-loop erase op once
                        _seen.add("e")
                        log.debug("install: erasing block XIP")
                log.debug("install: erased FRONT XIP")
                return  # hil-residual: bare return (ranged erase done)
            # Legacy single-shot fallback for firmware WITHOUT #19348 (no ranged prepare). The
            # bench always applies #19348, so rom_ioctl(6) returns a size and the ranged branch
            # above is taken; the single-shot erase is also the exact op that faults partway on
            # the N6's 12 MiB XSPI slot (the bug #19348 fixes), so it can't be exercised on HW.
            with relax():                             # hil-residual: legacy pre-#19348 single-shot erase (unreachable on the patched bench; faults the N6)
                rc = vfs.rom_ioctl(3, 0, total)  # hil-residual: legacy single-shot erase call (pre-#19348)
                if rc < 0:  # hil-residual: legacy single-shot errno check (pre-#19348)
                    raise OSError(-rc)  # hil-residual: bare raise (legacy single-shot erase fault)

        def write(off, data):
            rc = vfs.rom_ioctl(4, 0, off, data)
            if rc < 0:
                raise OSError(-rc)  # hil-residual: bare raise on a negative errno (write-fault, inject-only)
            if log and "w" not in _seen:
                _seen.add("w")
                log.debug("install: wrote block XIP")

        def complete():
            vfs.rom_ioctl(5, 0)                       # flush cached sub-page writes
            log.debug("install: complete XIP")
        log.debug("install: write path ready XIP")

    # Pre-erase: fetch + verify + vet the manifest, pick the image. Errors raise to the
    # app (the FRONT slot is untouched). Log the reason first: run() swallows this exception
    # (transient failures retry next poll), so without a line here a REJECTED update -- bad
    # signature, untrusted key, failed board/version/platform vetting -- is invisible in the
    # field, exactly when an operator most needs to know why a release won't take.
    # install() runs SYNCHRONOUSLY inside run()'s asyncio task, so the app's async feed loop can't run
    # during it -- every blocking op must feed the watchdog itself. The unsplittable network ops (TLS
    # handshake in _connect, each recv in _Reader) relax() at their leaf; the erase/write loops stay
    # PROGRESS-fed per block/piece, so a hung flash/reconstruct loop is still caught. All no-op off.
    log.info("install: fetching manifest %s" % manifest_url)
    try:
        image_url, fmt, expect_sha = _fetch_manifest(manifest_url, ca_pem, cfg, verify, socket, ssl, feed)
    except Exception as e:
        # Two very different failures land here and only ONE is a rejected update. A REJECTION -- bad
        # signature/key, failed vetting, or a corrupt/unparseable manifest -- must read as install.reject
        # (the tamper scenarios witness it). A TRANSPORT failure -- the link dropped/reset/timed out mid-
        # fetch -- is transient: it retries next poll and must NOT trip the reject gate after a clean
        # retry succeeds (a flaky link: WiFi power-save, the WINC's first post-checkin TLS, cellular).
        # _is_transport_error splits them on the errno (unit-tested); /rom is untouched either way.
        if _is_transport_error(e):
            log.warning("install: deferred, transport error (%r)" % e)  # hil-residual: transient transport defer -- a flaky link is NON-DETERMINISTIC (no marker can witness it); the classifier is unit-tested and the reject branch below IS witnessed (install.reject). Retries next poll
        else:
            log.warning("install: rejected before erase (%r)" % e)       # /rom untouched (%r shows the class)
        raise  # hil-residual: bare re-raise to the app (reject -> install.reject; transport -> retry next poll, marker-less)

    # Commit point: from the erase on we can't unwind into the (erased) app, so any
    # failure reboots into the golden image instead of propagating. ERASE FIRST,
    # THEN open the download: the whole-slot erase takes seconds, and if the socket
    # were already open it would sit idle that whole time -- a slow flash (the AE3's
    # external OSPI) on a power-saving WiFi link drops an idle connection, and the
    # write loop then reads a truncated body. Opening the download only after the
    # erase means it is read continuously. (A download-open failure here is rare --
    # the manifest was just fetched from the same server -- and lands cleanly in
    # golden.)
    # A flaky link (WiFi power-save, a slow OSPI flash, cellular) drops the download
    # mid-stream -- a transient transport error, not a bad update. Since the installer
    # already runs from RAM (exec'd before the erase) and re-erase + re-download is
    # idempotent, retry the whole download a bounded number of times before giving up.
    # Only an EXHAUSTED retry (or a non-transient failure surfacing every attempt)
    # reboots into golden BACK -- so one hiccup no longer costs a reboot + a full poll
    # cycle. Anything raised here (short body, TLS/ECONNRESET/ECONNABORTED/timeout,
    # verify miscompare) is treated the same: retry, then fall back.
    attempts = getattr(cfg, "INSTALL_RETRIES", 3)
    body = None
    for attempt in range(attempts):
        try:
            log.info("install: erasing FRONT (%d bytes)" % front_size)
            erase(front_size)
            log.info("install: downloading %s (%s)" % (image_url, fmt))
            sock, raw_body = _open(image_url, ca_pem, socket, ssl, feed)
            # Wrap the body so a dropped connection RESUMES at the compressed offset already
            # consumed instead of restarting the whole install (see _ResumingBody). The decoder
            # below keeps its state across the reconnect, so the stream it sees is continuous.
            # Cleanup below closes the BODY, not `sock`: after a resume the body owns a NEWER
            # socket and the original is already closed, so closing `sock` would leak the live one.
            body = _ResumingBody(image_url, ca_pem, socket, ssl, feed, sock, raw_body)
            dio = deflate.DeflateIO(body, deflate.GZIP)
            if fmt == _DELTA_FORMAT:
                # Delta: stream-decompress the patch and reconstruct the image against the
                # golden BACK slot (copy-with-diff, ulab add) -- both the patch and the output
                # are streamed into FRONT, neither is materialised.
                source = _GenReader(_delta_stream(_PatchReader(dio), back_read, _CHUNK), feed)
                repr_marker = REPR_DELTA
                log.debug("install: representation delta")
            else:
                source = dio                          # DeflateIO is itself a readinto source
                repr_marker = REPR_FULL
                log.debug("install: representation full")
            log.info("install: writing FRONT")
            _install_stream(source, write, readback, front_size, block, feed,
                            progress, expect_sha, repr_marker, gc_collect)
            # Commit the write. On the XIP/ioctl ports this is rom_ioctl(5), the
            # WRITE_COMPLETE flush (mpremote's romfs deploy ends the same way): those
            # ports cache the final sub-page writes -- the trailer + arm markers -- and
            # lose them at reset without it. Block-device ports persist on writeblocks,
            # so complete() there is a no-op.
            complete()
            log.debug("install: committed FRONT")
            break  # hil-residual: bare break (success -> arm + reboot; witnessed by install.committed)
        except Exception as e:
            if body is not None:
                body.close()                          # closes whichever socket the body now owns
                body = None
                log.debug("install: retry cleanup")  # HIL path witness (socket closed before a retry)
            if attempt + 1 >= attempts:
                log.error("install: FAILED after %d attempts (%r); rebooting to golden BACK"
                          % (attempts, e))                # %r: show the exception CLASS even when
                _reset()  # hil-residual: terminal reset to golden on retry exhaustion (witnessed by install.fallback + install.reboot)
            log.error("install: attempt %d/%d failed (%r); retrying"   # its message is empty (a bare
                      % (attempt + 1, attempts, e))                    # deflate OSError) -> legible in the field
            if progress is not None:  # hil-residual: progress callback is unused on the bench (install() passes none), so this guard is False
                progress.reset()  # hil-residual: progress reset (callback unused on the bench)
    log.info("install: installed + armed; rebooting into the trial")
    _reset()  # hil-residual: terminal reset into the trial on success (witnessed by install.armed + install.reboot)
