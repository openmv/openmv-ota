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
    import openmv_log
except ImportError:                    # host / tests / a build without logging
    openmv_log = None

try:                                   # ...and openmv_wdt (the watchdog helper)
    import openmv_wdt
except ImportError:
    openmv_wdt = None



class _NoWdt:  # pragma: no cover  (fallback relax() context when no watchdog is frozen)
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _noop():  # pragma: no cover  (fallback feed() when no watchdog is frozen)
    pass

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

# Socket timeout (s) for the download: bounds the TLS handshake and every recv so a
# stalled connection fails the install cleanly (-> reboot to golden) instead of hanging.
_SOCK_TIMEOUT = 30


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


def _request_bytes(host, port, path):
    """The HTTP/1.1 GET request line + headers. ``Connection: close`` so the server
    ends the body by closing -- and the gzip stream is self-terminating regardless."""
    hosthdr = host if port == 443 else "%s:%d" % (host, port)
    return ("GET %s HTTP/1.1\r\nHost: %s\r\n"
            "User-Agent: openmv-ota\r\nAccept: */*\r\nConnection: close\r\n\r\n"
            % (path, hosthdr)).encode()


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

    def __init__(self, recv, buf=b""):
        self._recv = recv
        self._buf = buf

    def _fill(self):
        d = self._recv(_CHUNK)
        if not d:
            return False
        self._buf += d
        return True

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
    if diff_b == bytes(len(diff_b)):
        return bytes(old_b)
    if _np is not None:
        return (_np.frombuffer(old_b, dtype=_np.uint8)        # pragma: no cover (device/ulab)
                + _np.frombuffer(diff_b, dtype=_np.uint8)).tobytes()
    return bytes((old_b[i] + diff_b[i]) & 0xFF for i in range(len(diff_b)))


class _PatchReader:
    """A buffered reader over a streamed patch source (``src.read(n)`` -- a DeflateIO),
    giving exact reads + varints so the patch is consumed in one forward pass."""

    def __init__(self, src):
        self._src = src
        self._buf = b""

    def _fill(self, need):
        while len(self._buf) < need:
            d = self._src.read(_CHUNK)
            if not d:
                return
            self._buf += d

    def read_exact(self, k):
        self._fill(k)
        if len(self._buf) < k:
            raise OSError("delta truncated")
        out, self._buf = self._buf[:k], self._buf[k:]
        return out

    def read_uvarint(self):
        result = shift = 0
        while True:
            self._fill(1)
            if not self._buf:
                raise OSError("delta truncated")
            b = self._buf[0]
            self._buf = self._buf[1:]
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result
            shift += 7

    def read_svarint(self):
        zz = self.read_uvarint()
        return (zz >> 1) if not (zz & 1) else -((zz + 1) >> 1)


def _delta_stream(reader, old_read, chunk):
    """Yield the reconstructed image in pieces from a streamed OCDL patch (via ``reader``)
    + the golden base (``old_read(off, n)`` over the XIP'd BACK slot). Mirror of
    openmv_ota.ota.delta.apply_delta -- streamed both ways, so neither the patch nor the
    target is held whole. Raises OSError on a bad/short patch (-> reboot to golden)."""
    if reader.read_exact(4) != _DELTA_MAGIC:
        raise OSError("bad delta magic")
    target_size = reader.read_uvarint()
    old = produced = 0
    while produced < target_size:
        extra_len = reader.read_uvarint()
        diff_len = reader.read_uvarint()
        old += reader.read_svarint()
        left = extra_len
        while left:                                  # chunked like the diff run
            m = left if left < chunk else chunk      # never one huge read_exact:
            yield reader.read_exact(m)               # extra_len is patch-declared
            produced += m                            # and only hash-checked after
            left -= m
        o = old
        left = diff_len
        while left:
            m = left if left < chunk else chunk
            yield _add(old_read(o, m), reader.read_exact(m))
            o += m
            left -= m
        old += diff_len
        produced += diff_len


class _GenReader:
    """Adapt a generator of byte pieces to the ``read(n)`` source ``_install_stream``
    pulls from -- buffers just enough to serve each request."""

    def __init__(self, gen):
        self._gen = gen
        self._buf = b""

    def read(self, n):
        while len(self._buf) < n:
            try:
                self._buf += bytes(next(self._gen))
            except StopIteration:
                break
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


# --- pure: the flash write (host-testable; all I/O injected) -----------------

def _is_blank(chunk):
    """True if ``chunk`` is all 0xFF -- already-erased flash we needn't rewrite."""
    return chunk == b"\xff" * len(chunk)


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


def _install_stream(read, write, readback, front_size, block, feed,
                    progress=None, expect_sha=None, repr_marker=None):
    """Stream the decompressed image into the ALREADY-ERASED FRONT slot 1:1
    (verifying every write by read-back, skipping already-erased 0xFF runs), then
    arm the trial.

    The caller MUST erase the FRONT slot BEFORE calling this AND before opening the
    download stream ``read`` draws from -- so the download socket is never left idle
    during the multi-second erase (a slow flash on a power-saving link drops an idle
    connection, and the write loop would then read a truncated body). This function
    starts by read-back verifying the slot is fully erased.

    ``read(n)`` yields decompressed image bytes (``b''`` at end); ``write(off, data)``
    programs flash; ``readback(off, n)`` returns the ``n`` bytes at ``off``; ``feed()``
    is called once per chunk so the watchdog stays alive through the loops *without*
    masking a hang (if the loop stops iterating, feeding stops); ``progress(done,
    front_size)`` (if given) is called once per written chunk so the caller can
    log/report how far the install has got; ``expect_sha`` (if given, the manifest's hex
    sha256 of the reconstructed image) is checked over the streamed bytes and must match;
    ``repr_marker`` (if given) records which representation was applied (REPR_FULL /
    REPR_DELTA) for status() to report. Raises on any size/hash mismatch or read-back
    miscompare; this runs after the erase, so the caller turns any exception into a
    reboot into golden."""
    off = 0
    while off < front_size:                          # confirm the caller's erase took
        n = _CHUNK if front_size - off >= _CHUNK else front_size - off
        if not _is_blank(readback(off, n)):
            raise OSError("erase verify failed at %d" % off)
        off += n
        feed()

    digest = hashlib.sha256() if expect_sha is not None else None
    off = 0
    buf = b""
    while True:
        data = read(_CHUNK)
        if data:
            buf += data
        if len(buf) >= _CHUNK or (not data and buf):
            chunk, buf = buf[:_CHUNK], buf[_CHUNK:]
            if off + len(chunk) > front_size:
                raise ValueError("image larger than the %d-byte slot" % front_size)
            if digest is not None:
                digest.update(chunk)
            if not _is_blank(chunk):                 # erased regions are already 0xFF
                write(off, chunk)
                if readback(off, len(chunk)) != chunk:
                    raise OSError("write verify failed at %d" % off)
            off += len(chunk)
            feed()
            if progress is not None:
                progress(off, front_size)
        if not data:
            break
    if off != front_size:
        raise ValueError("image is %d bytes, expected a full %d-byte slot"
                         % (off, front_size))
    # MicroPython's hashlib has no .hexdigest() (CPython-only) -- hexlify the raw
    # digest instead, so the check runs identically on-device and on the host.
    if digest is not None and binascii.hexlify(digest.digest()).decode() != expect_sha:
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
        sock.settimeout(_SOCK_TIMEOUT)               # so a stalled handshake/recv can't
        sock.connect(ai[-1])                         # block forever -> clean install error
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=ca_pem)
        return ctx.wrap_socket(sock, server_hostname=host)
    except Exception:
        sock.close()
        raise


def _open(url, ca_pem, socket, ssl, max_redirects=5):  # pragma: no cover
    """Connect, GET, follow redirects, and return ``(sock, body)`` on a 2xx -- all
    before the erase, so a bad URL / DNS / TLS / non-2xx status raises to the app
    with /rom still intact."""
    for _ in range(max_redirects + 1):
        host, port, path = _parse_url(url)
        sock = _connect(host, port, ca_pem, socket, ssl)
        try:
            sock.write(_request_bytes(host, port, path))
            reader = _Reader(sock.read)
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
        return sock, _make_body(reader, headers)
    raise OSError("too many redirects")


def _fetch_manifest(manifest_url, ca_pem, cfg, verify, socket, ssl):  # pragma: no cover
    """Pre-erase: fetch the signed manifest, verify its signature against the frozen
    trusted keys (exactly as boot.py verifies an image trailer), apply the device-relative
    checks (board / platform / anti-rollback), and pick a representation. Returns
    ``(image_url, fmt, expect_sha)``. Raises (to the app, /rom intact) on any failure --
    nothing is erased."""
    import uctypes
    import vfs

    sock, body = _open(manifest_url, ca_pem, socket, ssl)
    try:
        raw = _read_all(body, _MANIFEST_MAX)
    finally:
        sock.close()
    m = _manifest_parse(raw)                          # structure + crc (raises on bad)
    pubkey = cfg.TRUSTED_KEYS.get(m["key_id"])
    if pubkey is None:
        raise OSError("manifest signed by an untrusted key")
    if not verify(m["sig_alg"], pubkey, m["signature"], m["region"]):
        raise OSError("manifest signature does not verify")

    body_dict = m["body"]
    base = uctypes.addressof(vfs.rom_ioctl(2, 0))     # partition XIP base
    floor = _golden_floor(uctypes.bytearray_at(base + cfg.PARTITION_SIZE - cfg.OTA_BLOCK,
                                              cfg.OTA_BLOCK))
    reason = _update_reject(body_dict, cfg.PRODUCT_ID, cfg.PLATFORM_VERSION, floor,
                            getattr(cfg, "ACCOUNT_ID", ""))
    if reason is not None:
        raise OSError("manifest rejected (%s)" % reason)
    # The delta applier is pure Python (no ulab/C), so every board is delta-capable; the
    # delta is used only when its base matches this device's golden (BACK) version.
    rep = _select_rep(body_dict, True, floor)
    if rep is None:
        raise OSError("manifest has no usable representation")
    return _resolve_url(manifest_url, rep["url"]), rep.get("format"), body_dict.get("sha256")


def _reset():  # pragma: no cover
    """Reboot into the freshly-selected slot, but let the frozen logger's handler drain
    first. ``machine.reset()`` cuts an in-flight UART TX, so the final line -- e.g.
    'installed + armed' -- is truncated mid-string and lost on a side-channel UART (the FIFO
    is only tens of bytes; most of the line is still buffered at reset). A short settle drains
    it. Harmless in production: a reboot is never time-critical, and it makes the last log line
    reliably land wherever the logger points (UART/socket/REPL)."""
    import time
    import machine
    time.sleep_ms(50)
    machine.reset()


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

    log = openmv_log.log if openmv_log is not None else None
    # Watchdog (if the app enabled one): relax() feeds it from a timer ISR ONLY around the
    # single multi-second erase the main loop can't reach; feed() keeps it alive per chunk
    # through the loops -- so a hung loop (or a stalled recv) still trips it -> golden.
    relax = openmv_wdt.relax if openmv_wdt is not None else _NoWdt
    feed = openmv_wdt.feed if openmv_wdt is not None else _noop
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
    if hasattr(front, "ioctl"):                       # block-device romfs (e.g. mimxrt)
        if log:
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

        def write(off, data):                         # extended writeblocks: byte-granular,
            front.writeblocks(off // _bs, data, off % _bs)   # so sub-block markers work too

        def readback(off, n):
            b = bytearray(n)                          # n <= _CHUNK: a bounded readback buffer.
            front.readblocks(off // _bs, b, off % _bs)  # FRONT at partition offset off
            return b

        def back_read(off, n):                        # arbitrary range from BACK, block-safe
            out = bytearray(n)                        # BACK lives at front_size within the one
            done = 0                                  # partition (NOT a separate rom_ioctl(2,1)
            while done < n:                           # segment -- that returns FRONT on mimxrt)
                a = front_size + off + done
                blk, o = a // _bs, a % _bs
                take = _bs - o
                if take > n - done:
                    take = n - done
                front.readblocks(blk, memoryview(out)[done:done + take], o)
                done += take
            return out

        def complete():
            pass                                      # writeblocks persists; no flush ioctl

    else:                                             # XIP-mapped romfs (stm32/alif/samd)
        if log:
            log.debug("install: write path XIP")
        base = uctypes.addressof(front)               # FRONT partition XIP base

        def readback(off, n):
            return uctypes.bytearray_at(base + off, n)

        def back_read(off, n):
            return uctypes.bytearray_at(base + front_size + off, n)   # BACK at front_size

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
                        raise OSError(-rc)
                    o += n
                    feed()
                return
            with relax():                             # the one op we can't feed in a loop
                rc = vfs.rom_ioctl(3, 0, total)
                if rc < 0:
                    raise OSError(-rc)

        def write(off, data):
            rc = vfs.rom_ioctl(4, 0, off, data)
            if rc < 0:
                raise OSError(-rc)

        def complete():
            vfs.rom_ioctl(5, 0)                       # flush cached sub-page writes

    # Pre-erase: fetch + verify + vet the manifest, pick the image. Errors raise to the
    # app (the FRONT slot is untouched).
    if log:
        log.info("install: fetching manifest %s" % manifest_url)
    image_url, fmt, expect_sha = _fetch_manifest(manifest_url, ca_pem, cfg, verify, socket, ssl)

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
    sock = None
    for attempt in range(attempts):
        try:
            if log:
                log.info("install: erasing FRONT (%d bytes)" % front_size)
            erase(front_size)
            if log:
                log.info("install: downloading %s (%s)" % (image_url, fmt))
            sock, body = _open(image_url, ca_pem, socket, ssl)
            dio = deflate.DeflateIO(body, deflate.GZIP)
            if fmt == _DELTA_FORMAT:
                # Delta: stream-decompress the patch and reconstruct the image against the
                # golden BACK slot (copy-with-diff, ulab add) -- both the patch and the output
                # are streamed into FRONT, neither is materialised.
                source = _GenReader(_delta_stream(_PatchReader(dio), back_read, _CHUNK)).read
                repr_marker = REPR_DELTA
                if log:
                    log.debug("install: representation delta")
            else:
                source = dio.read
                repr_marker = REPR_FULL
                if log:
                    log.debug("install: representation full")
            if log:
                log.info("install: writing FRONT")
            _install_stream(source, write, readback, front_size, block, feed,
                            progress, expect_sha, repr_marker)
            # Commit the write. On the XIP/ioctl ports this is rom_ioctl(5), the
            # WRITE_COMPLETE flush (mpremote's romfs deploy ends the same way): those
            # ports cache the final sub-page writes -- the trailer + arm markers -- and
            # lose them at reset without it. Block-device ports persist on writeblocks,
            # so complete() there is a no-op.
            complete()
            break                                    # success -> arm + reboot into the trial
        except Exception as e:
            if sock is not None:
                sock.close()
                sock = None
            if attempt + 1 >= attempts:
                if log:
                    log.error("install: FAILED after %d attempts (%s); rebooting to golden BACK"
                              % (attempts, e))
                _reset()
            if log:
                log.error("install: attempt %d/%d failed (%s); retrying"
                          % (attempt + 1, attempts, e))
            if progress is not None:
                progress.reset()                     # restart % for the fresh re-download
    if log:
        log.info("install: installed + armed; rebooting into the trial")
    _reset()
