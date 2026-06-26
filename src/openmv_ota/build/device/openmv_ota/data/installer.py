"""The OTA installer -- downloads a gzipped FRONT-slot image over HTTPS and writes
it into the FRONT slot, then arms the trial and reboots.

This file ships in the romfs as **source** (it is exempt from the .py->.mpy build
step) so ``openmv_ota.install()`` can ``exec()`` it into RAM before the FRONT slot
is erased: the running app's code lives in that slot, so once the erase starts
nothing on it can be executed -- but this module, compiled into RAM by ``exec``,
runs from RAM throughout. ``run()`` never returns: on success it sets PENDING and
``machine.reset()``s into the trial; on any post-erase failure it resets into the
golden BACK image (boot.py rejects the half-written FRONT). Pre-erase failures
(bad URL, DNS, TLS, HTTP status) raise normally -- ``/rom`` is still intact, so the
app catches them and can retry without a reboot.

Like ``boot.py`` this is split into pure logic (URL/HTTP/chunked parsing, the
flash write loop -- all I/O injected, host-tested) and a device entry (``run`` /
``_open`` / ``_connect``) that wires the real ``socket``/``ssl``/``deflate``/
``vfs``/``machine`` and is excluded from host coverage. ``hashlib`` is the only
import that runs on the host (to derive the PENDING marker, pinned against
``openmv_ota.ota.status`` by a test); the device imports are lazy.
"""

import hashlib
import io

try:                                   # the firmware freezes openmv_log beside boot.py
    import openmv_log
except ImportError:                    # host / tests / a build without logging
    openmv_log = None

# --- Status marker (mirror of openmv_ota.ota.status; pinned by a test) -------

MARKER_SIZE = 16


def _marker(label):
    return hashlib.sha256(b"openmv-ota.status." + label).digest()[:MARKER_SIZE]


PENDING = _marker(b"pending")

# Stream/flash unit. FRONT_SIZE is always a multiple of this (it is block-aligned
# and the block is >= 4096), so every flash write is a full, aligned chunk.
_CHUNK = 4096


# --- pure: URL + HTTP (host-testable) ---------------------------------------

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


# --- pure: the flash write (host-testable; all I/O injected) -----------------

def _is_blank(chunk):
    """True if ``chunk`` is all 0xFF -- already-erased flash we needn't rewrite."""
    return chunk == b"\xff" * len(chunk)


def _install_stream(read, erase, write, readback, front_size, block):
    """Erase the FRONT slot, stream the decompressed image into it 1:1 (verifying
    every write by read-back, skipping already-erased 0xFF runs), then arm the trial.

    ``read(n)`` yields decompressed image bytes (``b''`` at end); ``erase(total)``
    erases ``total`` bytes from offset 0; ``write(off, data)`` programs flash;
    ``readback(off, n)`` returns the ``n`` bytes at ``off``. Raises on any size
    mismatch or read-back miscompare; this runs after the erase, so the caller turns
    any exception into a reboot into the golden image."""
    erase(front_size)
    off = 0
    while off < front_size:                          # confirm the erase took
        n = _CHUNK if front_size - off >= _CHUNK else front_size - off
        if not _is_blank(readback(off, n)):
            raise OSError("erase verify failed at %d" % off)
        off += n

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
            if not _is_blank(chunk):                 # erased regions are already 0xFF
                write(off, chunk)
                if readback(off, len(chunk)) != chunk:
                    raise OSError("write verify failed at %d" % off)
            off += len(chunk)
        if not data:
            break
    if off != front_size:
        raise ValueError("image is %d bytes, expected a full %d-byte slot"
                         % (off, front_size))

    pending_off = front_size - 2 * block             # the status sector
    write(pending_off, PENDING)                       # arm the one-shot trial, last
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
        sock.connect(ai[-1])
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


def run(url, ca_pem, cfg):  # pragma: no cover
    """Download and install the gzipped FRONT-slot image at ``url``. Never returns:
    reboots into the new image's trial on success, or into the golden BACK image if
    anything fails after the erase commits."""
    import deflate
    import machine
    import socket
    import ssl

    import uctypes
    import vfs

    log = openmv_log.log if openmv_log is not None else None
    front_size, block = cfg.FRONT_SIZE, cfg.OTA_BLOCK
    base = uctypes.addressof(vfs.rom_ioctl(2, 0))     # FRONT partition XIP base

    def readback(off, n):
        return uctypes.bytearray_at(base + off, n)

    def erase(total):
        rc = vfs.rom_ioctl(3, 0, total)
        if rc < 0:
            raise OSError(-rc)

    def write(off, data):
        rc = vfs.rom_ioctl(4, 0, off, data)
        if rc < 0:
            raise OSError(-rc)

    # Pre-erase: connect + verify TLS + read response headers. Errors here raise to
    # the app (the FRONT slot is untouched).
    if log:
        log.info("install: downloading %s" % url)
    sock, body = _open(url, ca_pem, socket, ssl)

    # Commit point: from the erase on we can't unwind into the (erased) app, so any
    # failure reboots into the golden image instead of propagating.
    if log:
        log.info("install: connected; erasing + writing FRONT (%d bytes)" % front_size)
    try:
        dio = deflate.DeflateIO(body, deflate.GZIP)
        _install_stream(dio.read, erase, write, readback, front_size, block)
    except Exception as e:
        sock.close()
        if log:
            log.error("install: FAILED after erase (%s); rebooting to golden BACK" % e)
        machine.reset()
    sock.close()
    if log:
        log.info("install: installed + armed; rebooting into the trial")
    machine.reset()
