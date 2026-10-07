"""``openmv_ota.se.se050`` -- the NXP EdgeLock SE050 (OpenMV RT1062, Arduino Nicla Vision and
Portenta H7): T=1 over I2C and the applet commands the camera's identity needs.

Every SE050C leaves NXP's factory with a die-individual P-256 key pair at ``0xF0000000`` that
signs any digest and cannot be erased, and its certificate at ``0xF0000001``, issued by "NXP
Intermediate-ConnectivityCAvE206". That pair is the identity: nothing is provisioned on our
side. (NXP AN12436 rev 2.4 table 12; read and signature-checked on an RT1062.)

The link is ISO 7816-3 T=1 framed for I2C (NXP UM11225): ``NAD | PCB | LEN | INF | CRC16``, the
host sending NAD ``0x5A`` and the chip answering ``0xA5``; the chip NACKs its address while it
works. APDUs are NXP AN12413's: CLA ``0x80``, TLV payloads, a TLV-wrapped answer and SW1 SW2.

A half-read reply wedges the chip. If the camera resets in the middle of an exchange, the
SE050 keeps waiting to send the rest; a session started over it fails, and retrying that (or
clocking SCL) leaves the chip NACKing its address until power is cut -- on a board with no
enable pin, a power cycle by hand. So every session first DRAINS: it reads single bytes until
the chip NACKs, then starts afresh (measured on an RT1062: 4 bytes pending, then a clean start).

RAM BUDGET: this module runs inside your application, so its memory is your memory. Two
buffers, a frame and an answer, are allocated once; a certificate read allocates the
certificate (470 bytes) and is capped at ``_CERT_MAX``.
"""

import time

_ADDR = 0x48
_NAD = 0x5A                # host -> chip (the chip answers 0xA5)
_IFS = 254                 # most INF bytes in one frame, either way (the ATR's IFSC)
_KEY = 0xF0000000          # the factory identity key pair
_CERT = 0xF0000001         # ...and its NXP certificate
_CHUNK = 200               # bytes per object read: one frame each way
_CERT_MAX = 2048
_DRAIN_MAX = 512           # bytes read off a reply someone else abandoned, at most
_WAIT_MS = 1000            # how long a reply may take: the ATR's BWT (1 s on the SE050C)
_AID = b"\xa0\x00\x00\x03\x96\x54\x53\x00\x00\x00\x01\x03\x00\x00\x00\x00"   # the IoT applet


def _crc(buf, n):
    """CRC-16/X-25 over ``buf[:n]`` (reflected 0x8408, init and final XOR 0xFFFF). Pure."""
    crc = 0xFFFF
    for i in range(n):
        crc ^= buf[i]
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def _put_u32(buf, off, v):
    buf[off] = v >> 24
    buf[off + 1] = (v >> 16) & 0xFF
    buf[off + 2] = (v >> 8) & 0xFF
    buf[off + 3] = v & 0xFF


def _der_len(cert):
    """The length of the DER SEQUENCE at the start of ``cert`` -- certificate objects are
    zero-padded to a fixed size. Pure."""
    n = cert[1]
    if n < 0x80:
        return 2 + n
    k = n & 0x7F
    return 2 + k + int.from_bytes(cert[2:2 + k], "big")


class SecureElement:
    """The SE050 on ``i2c`` (a ``machine.I2C``), powered by ``enable`` (a ``machine.Pin``, or
    None where it is always on). See :mod:`openmv_ota.se` for the interface."""

    def __init__(self, i2c, addr=_ADDR, enable=None):
        self._i2c = i2c
        self._addr = addr
        self._frame = bytearray(3 + _IFS + 2)
        self._ans = bytearray(_IFS + 2)
        self._mv = memoryview(self._ans)
        self._ns = 0                              # N(S) of our next I-block
        if enable is not None:                    # a clean power-up: no state to drain
            enable(0)
            time.sleep_ms(10)
            enable(1)
            time.sleep_ms(10)
        self._start()

    # -- the commands --------------------------------------------------------------

    def public_key(self):
        """The identity key's public half: 65 bytes, ``04 || X || Y``."""
        return bytes(self._apdu(0x02, 0x00, 0x00, self._tlv_id(_KEY)))

    def certificate(self):
        """The identity key's DER X.509 certificate, issued by NXP."""
        size = int.from_bytes(self._apdu(0x02, 0x00, 0x07, self._tlv_id(_CERT)), "big")
        if size > _CERT_MAX:
            raise OSError("se050: certificate of %d bytes" % size)
        cert = bytearray(size)
        off = 0
        while off < size:
            n = min(_CHUNK, size - off)
            f = self._frame
            k = self._tlv_id(_CERT)
            f[k:k + 8] = b"\x42\x02\x00\x00\x43\x02\x00\x00"
            f[k + 2], f[k + 3], f[k + 6], f[k + 7] = off >> 8, off & 0xFF, n >> 8, n & 0xFF
            cert[off:off + n] = self._apdu(0x02, 0x00, 0x00, k + 8)
            off += n
        return bytes(cert[:_der_len(cert)])

    def sign(self, digest):
        """An ECDSA P-256 signature, DER-encoded, over the 32-byte SHA-256 ``digest``."""
        if len(digest) != 32:
            raise ValueError("digest must be 32 bytes")
        f = self._frame
        k = self._tlv_id(_KEY)
        f[k:k + 5] = b"\x42\x01\x21\x43\x20"     # ECSignatureAlgo SHA-256, then the digest
        f[k + 5:k + 37] = digest
        return bytes(self._apdu(0x03, 0x0C, 0x09, k + 37))

    def random(self, n):
        """``n`` (at most 200) random bytes from the chip's TRNG."""
        if not 0 < n <= _CHUNK:
            raise ValueError("1..200 bytes at a time")
        f = self._frame
        f[8:12] = b"\x41\x02\x00\x00"
        f[11] = n
        return bytes(self._apdu(0x04, 0x00, 0x49, 12))

    # -- the APDU layer ----------------------------------------------------------

    def _tlv_id(self, oid):
        """TAG_1 = ``oid`` written as the APDU's first TLV; the frame offset after it."""
        f = self._frame
        f[8] = 0x41
        f[9] = 4
        _put_u32(f, 10, oid)
        return 14

    def _apdu(self, ins, p1, p2, end, cla=0x80):
        """Send the APDU whose TLV payload is ``frame[8:end]``; return a view of its answer's
        value (the first TLV's, if it has one -- SELECT's has none) after checking SW 9000."""
        f = self._frame
        lc = end - 8
        f[3], f[4], f[5], f[6], f[7] = cla, ins, p1, p2, lc
        f[end] = 0                                # Le
        n = self._exchange(lc + 6)
        sw = (self._ans[n - 2] << 8) | self._ans[n - 1]
        if sw != 0x9000:
            raise OSError("se050: SW %04X" % sw)
        if n == 2 or cla == 0x00:
            return self._mv[:n - 2]
        k = 2
        size = self._ans[1]
        if size == 0x81:
            size = self._ans[2]
            k = 3
        elif size == 0x82:
            size = (self._ans[2] << 8) | self._ans[3]
            k = 4
        return self._mv[k:k + size]

    # -- T=1 over I2C ------------------------------------------------------------

    def _start(self):
        """A fresh session: drain any abandoned reply, soft-reset the link (answered with the
        ATR), select the applet."""
        for _ in range(_DRAIN_MAX):
            try:
                self._i2c.readfrom(self._addr, 1)
            except OSError:
                break
        self._send(0xCF, 0)                       # S(interface soft reset)
        if self._recv() != 0xEF:
            raise OSError("se050: no ATR")
        self._ns = 0
        f = self._frame
        f[8:8 + len(_AID)] = _AID
        self._apdu(0xA4, 0x04, 0x00, 8 + len(_AID), cla=0x00)

    def _send(self, pcb, n):
        """Frame ``frame[3:3+n]`` as a block with ``pcb`` and write it."""
        f = self._frame
        f[0], f[1], f[2] = _NAD, pcb, n
        c = _crc(f, 3 + n)
        f[3 + n] = c & 0xFF
        f[4 + n] = c >> 8
        self._i2c.writeto(self._addr, memoryview(f)[:5 + n])

    def _recv(self, wait_ms=_WAIT_MS):
        """Read one block into the frame buffer, polling while the chip NACKs; its PCB."""
        f = self._frame
        mv = memoryview(f)
        deadline = time.ticks_add(time.ticks_ms(), wait_ms)
        nap = 1
        while True:
            try:
                self._i2c.readfrom_into(self._addr, mv[:3])
                if f[0] == 0xA5:
                    break
            except OSError:
                # NACKed: still working. Each NACK is an exception object, so back off rather
                # than poll every millisecond -- a 48 ms signature cost ~3 KiB of them.
                pass
            if time.ticks_diff(deadline, time.ticks_ms()) < 0:
                raise OSError("se050: no answer")
            time.sleep_ms(nap)
            nap = min(nap * 2, 8)
        n = f[2]
        self._i2c.readfrom_into(self._addr, mv[3:5 + n])
        if _crc(f, 3 + n) != f[3 + n] | (f[4 + n] << 8):
            raise OSError("se050: bad CRC")
        return f[1]

    def _exchange(self, n):
        """Send ``frame[3:3+n]`` as an I-block and gather the reply's INF into ``_ans``;
        how many bytes. Answers a chained reply's blocks and the chip's wait-time requests."""
        self._send(self._ns << 6, n)
        self._ns ^= 1
        got = 0
        wait = _WAIT_MS
        while True:
            pcb = self._recv(wait)
            wait = _WAIT_MS
            f = self._frame
            size = f[2]
            if not pcb & 0x80:                    # I-block: (part of) the answer
                if got + size > len(self._ans):
                    raise OSError("se050: answer too long")
                self._ans[got:got + size] = memoryview(f)[3:3 + size]
                got += size
                if not pcb & 0x20:
                    return got
                self._send(0x80 | (((pcb >> 2) & 0x10) ^ 0x10), 0)   # R(N(R)): next please
            elif pcb == 0xC3:                     # S(WTX request): more time
                wait = _WAIT_MS * f[3]
                self._send(0xE3, 1)               # S(WTX response), echoing the multiplier
            else:
                raise OSError("se050: unexpected block %02X" % pcb)
