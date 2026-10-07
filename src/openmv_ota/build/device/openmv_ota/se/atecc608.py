"""``openmv_ota.se.atecc608`` -- the Microchip ATECC608 (Arduino Giga R1 WiFi): its I2C command
protocol and the commands the camera's identity needs.

Unlike the SE050 this chip leaves Microchip's factory BLANK: no key, no certificate, its
configuration and data zones unlocked. It can only generate and sign once someone has written
its configuration and locked both zones -- one-way, for the life of the chip -- which is
provisioning, not something this module does. Until then :class:`SecureElement` refuses to open
("not provisioned") rather than hand out the fixed pattern an unlocked chip returns for random.
The identity is then the P-256 key in slot 0 (Arduino's layout: private, never readable, signs
external digests); it carries no maker's certificate, so :meth:`SecureElement.certificate` is
None and the server learns the key when the camera is first registered.

The chip sleeps; every exchange starts with a WAKE -- SDA held low >= 60 us, done by addressing
0x00 at 100 kHz -- answered ``04 11 33 43``, and ends by sending it to idle. A command is
``03 | count | opcode | param1 | param2 (LE) | data | CRC16 (LE)``, the CRC polynomial 0x8005
fed least-significant bit first; the chip NACKs while it executes. (Microchip cryptoauthlib
and Arduino's ArduinoECCX08; framing checked against a Giga's chip.)

RAM BUDGET: this module runs inside your application, so its memory is your memory. One packet
buffer is allocated once; a command's answer is at most 64 bytes.
"""

import time

_ADDR = 0x60
_WAKE = b"\x04\x11\x33\x43"
_SLOT = 0                         # the identity key's slot
_WAIT_MS = 1500                   # a command's slowest case (GenKey/Sign at a divided clock)


def _crc(buf, start, end):
    """The ATECC CRC-16 over ``buf[start:end]``: polynomial 0x8005, no init or final XOR, each
    byte fed least-significant bit first. Pure."""
    crc = 0
    for i in range(start, end):
        b = buf[i]
        for k in range(8):
            if ((b >> k) & 1) ^ (crc >> 15):
                crc = ((crc << 1) ^ 0x8005) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _der_int(v):
    v = bytes(v).lstrip(b"\x00") or b"\x00"
    if v[0] & 0x80:
        v = b"\x00" + v
    return b"\x02" + bytes([len(v)]) + v


def _der_sig(rs):
    """A raw ``r || s`` signature (64 bytes) as DER ``SEQUENCE { INTEGER r, INTEGER s }`` -- what
    every secure element here returns, whatever the chip's own format. Pure."""
    body = _der_int(rs[:32]) + _der_int(rs[32:64])
    return b"\x30" + bytes([len(body)]) + body


class SecureElement:
    """The ATECC608 on ``i2c`` (a ``machine.I2C``, at most 100 kHz so the wake pulse is long
    enough). ``enable`` is unused: the chip has no enable pin. Raises OSError if the chip is
    not provisioned. See :mod:`openmv_ota.se` for the interface."""

    def __init__(self, i2c, addr=_ADDR, enable=None):
        self._i2c = i2c
        self._addr = addr
        self._pkt = bytearray(8 + 32 + 2)
        lock = self._run(0x02, 0x00, 0x15)        # config word 0x15: ..., LockValue, LockConfig
        if lock[2] or lock[3]:                    # 0x00 = locked, 0x55 = unlocked
            raise OSError("atecc608: not provisioned (zones unlocked)")

    def public_key(self):
        """The identity key's public half: 65 bytes, ``04 || X || Y``."""
        return b"\x04" + self._run(0x40, 0x00, _SLOT)       # GenKey: public key of the slot

    def certificate(self):
        """None: this chip carries no maker's certificate."""
        return None

    def sign(self, digest):
        """An ECDSA P-256 signature, DER-encoded, over the 32-byte SHA-256 ``digest``."""
        if len(digest) != 32:
            raise ValueError("digest must be 32 bytes")
        # Nonce pass-through puts the digest in TempKey; Sign (external) signs TempKey. Both in
        # one wake: TempKey does not survive the chip going back to sleep.
        return _der_sig(self._run(0x41, 0x80, _SLOT, nonce=digest))

    def random(self, n):
        """``n`` (at most 32) random bytes from the chip's TRNG."""
        if not 0 < n <= 32:
            raise ValueError("1..32 bytes at a time")
        return self._run(0x1B, 0x00, 0x0000)[:n]

    # -- the chip's command protocol ------------------------------------------------------

    def _run(self, op, p1, p2, nonce=None):
        """Wake the chip, run one command (after a Nonce pass-through of ``nonce``, if given),
        send it to idle; the command's answer."""
        self._wake()
        try:
            if nonce is not None:
                self._cmd(0x16, 0x03, 0x0000, nonce)
            return self._cmd(op, p1, p2)
        finally:
            try:
                self._i2c.writeto(self._addr, b"\x02")      # idle: keep the chip's state light
            except OSError:
                pass

    def _wake(self):
        try:
            self._i2c.writeto(0x00, b"\x00")               # SDA low long enough: a wake pulse
        except OSError:
            pass                                          # nobody answers address 0: expected
        time.sleep_us(1500)
        if self._i2c.readfrom(self._addr, 4) != _WAKE:
            raise OSError("atecc608: no wake")

    def _cmd(self, op, p1, p2, data=b""):
        p = self._pkt
        n = 7 + len(data)
        p[0], p[1], p[2], p[3], p[4], p[5] = 0x03, n, op, p1, p2 & 0xFF, p2 >> 8
        p[6:6 + len(data)] = data
        c = _crc(p, 1, n - 1)
        p[n - 1] = c & 0xFF
        p[n] = c >> 8
        self._i2c.writeto(self._addr, memoryview(p)[:n + 1])
        deadline = time.ticks_add(time.ticks_ms(), _WAIT_MS)
        nap = 1
        while True:
            try:
                count = self._i2c.readfrom(self._addr, 1)[0]
                break
            except OSError:                               # executing: it NACKs
                if time.ticks_diff(deadline, time.ticks_ms()) < 0:
                    raise OSError("atecc608: no answer")
                time.sleep_ms(nap)
                nap = min(nap * 2, 16)
        if not 4 <= count <= 3 + 64:
            raise OSError("atecc608: bad length %d" % count)
        r = bytearray(count)
        r[0] = count
        r[1:] = self._i2c.readfrom(self._addr, count - 1)
        if _crc(r, 0, count - 2) != r[count - 2] | (r[count - 1] << 8):
            raise OSError("atecc608: bad CRC")
        if count == 4 and r[1]:
            raise OSError("atecc608: status %02X" % r[1])
        return bytes(r[1:count - 2])
