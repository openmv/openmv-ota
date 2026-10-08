"""``openmv_ota.se.atecc608`` -- the Microchip ATECC608 (Arduino Giga R1 WiFi): its I2C command
protocol and the commands the camera's identity needs.

Unlike the SE050 this chip leaves Microchip's factory BLANK: no key, its configuration and data
zones unlocked. :meth:`SecureElement.provision` -- a desk step (see :mod:`openmv_ota.se`) --
makes it ready, every step leaving the chip exactly as Arduino's own Cloud provisioning would
(ArduinoECCX08's ``ECCX08_DEFAULT_TLS_CONFIG``, then both locks), so the board still onboards
to Arduino Cloud:

1. write Arduino's configuration, read it back and compare (refusing a chip someone else
   configured), and lock it -- with the chip checking the CRC of what it locks;
2. lock the data zone;
3. generate the identity key in slot 2 and the exchange key in slot 3, then write a record of
   both to slot 8.

The locks are one-way, for the life of the chip; the keys are not -- those slots allow GenKey
again after the locks. Each step checks the chip's state first, so a power cut part way resumes
on the next provisioning. Slots 2, 3 and 8 are slots Arduino never touches: its Cloud onboarding
regenerates the key in slot 0 every time it runs, so an identity there would not survive it.
The record in slot 8 marks the keys as made; until it is written they are made again, so keys
are never reported that a later provisioning replaces. Opening only reads: a chip without the
record raises :class:`NotProvisioned`. The chip carries no maker's certificate, so
:meth:`SecureElement.certificate` is None and the server learns the key when the camera is
registered.

The chip sleeps; every exchange starts with a WAKE -- SDA held low >= 60 us, done by addressing
0x00 at 100 kHz -- answered ``04 11 33 43``, and ends by sending it to idle. A command is
``03 | count | opcode | param1 | param2 (LE) | data | CRC16 (LE)``, the CRC polynomial 0x8005
fed least-significant bit first; the chip NACKs while it executes. (Microchip cryptoauthlib
and Arduino's ArduinoECCX08; framing and provisioning checked against a Giga's chip.)

RAM BUDGET: this module runs inside your application, so its memory is your memory. One packet
buffer is allocated once; a command's answer is at most 64 bytes. Provisioning, once in the
chip's life, reads the 128-byte configuration back.
"""

import hashlib
import time

_ADDR = 0x60
_WAKE = b"\x04\x11\x33\x43"
_SLOT = 2                         # the identity key's slot (Arduino uses 0 and 1)
_KX_SLOT = 3                      # the exchange key's slot
_REC = 8                          # the record that the keys were made (a clear slot Arduino leaves)
_MAGIC = b"OMVK\x01"
_WAIT_MS = 1500                   # a command's slowest case (GenKey/Sign at a divided clock)

# Configuration bytes 16-127 as Arduino writes them (ECCX08_DEFAULT_TLS_CONFIG). Bytes 0-15 are
# the chip's own; bytes 68-71 here (config 84-87: UserExtra, Selector and the two lock bytes) are
# set only by commands, so they are never written and never compared.
_CONFIG = bytes((
    0xC0, 0x00, 0x55, 0x00, 0x83, 0x20, 0x87, 0x20, 0x87, 0x20, 0x87, 0x2F, 0x87, 0x2F, 0x8F, 0x8F,
    0x9F, 0x8F, 0xAF, 0x8F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0xAF, 0x8F, 0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF,
    0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x55, 0x55, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x33, 0x00, 0x33, 0x00, 0x33, 0x00, 0x33, 0x00, 0x33, 0x00, 0x1C, 0x00, 0x1C, 0x00, 0x1C, 0x00,
    0x3C, 0x00, 0x3C, 0x00, 0x3C, 0x00, 0x3C, 0x00, 0x3C, 0x00, 0x3C, 0x00, 0x3C, 0x00, 0x1C, 0x00,
))


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


class NotProvisioned(OSError):
    """The chip has no keys of ours yet: the camera was never provisioned."""


class SecureElement:
    """The ATECC608 on ``i2c`` (a ``machine.I2C``, at most 100 kHz so the wake pulse is long
    enough). ``enable`` is unused: the chip has no enable pin. Raises :class:`NotProvisioned`
    if the camera was never provisioned. See :mod:`openmv_ota.se` for the interface."""

    @classmethod
    def provision(cls, i2c, addr=_ADDR, enable=None):
        """``(keys, made)``: on a chip without our keys, configure and lock it as Arduino would
        (if it isn't already) and make the keys, then open. ``made`` is False if it had them.
        Raises, changing nothing, on a chip someone else configured. A desk step."""
        se = cls(i2c, addr, enable, _make=True)
        return se, se._made

    def __init__(self, i2c, addr=_ADDR, enable=None, _make=False):
        self._i2c = i2c
        self._addr = addr
        self._pkt = bytearray(8 + 64 + 2)          # the largest command: ECDH, a 64-byte point
        self._made = False
        lock = self._run(0x02, 0x00, 0x15)        # config word 0x15: ..., LockValue, LockConfig
        # 0x00 = locked, 0x55 = unlocked. Provisioned = data locked and the keys' record there.
        if lock[2] or self._run(0x02, 0x82, _REC << 3)[:5] != _MAGIC:
            if not _make:
                raise NotProvisioned("atecc608: this camera has no keys; provision it")
            self._provision(lock[2], lock[3])
            self._made = True

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

    def ecdh_public_key(self):
        """The exchange key's public half: 65 bytes, ``04 || X || Y``."""
        return b"\x04" + self._run(0x40, 0x00, _KX_SLOT)

    def ecdh(self, peer):
        """The 32-byte ECDH secret of the exchange key and ``peer`` (65 bytes, ``04 || X || Y``),
        computed on the chip (ECDH; slot 3's config puts the secret in the clear output)."""
        if len(peer) != 65 or peer[0] != 4:
            raise ValueError("peer must be a 65-byte uncompressed public key")
        return self._run(0x43, 0x00, _KX_SLOT, peer[1:])

    def random(self, n):
        """``n`` (at most 32) random bytes from the chip's TRNG."""
        if not 0 < n <= 32:
            raise ValueError("1..32 bytes at a time")
        return self._run(0x1B, 0x00, 0x0000)[:n]

    # -- provisioning, at a desk ---------------------------------------------------------

    def _provision(self, data_open, config_open):
        if config_open:
            for i in range(0, 112, 4):
                if i != 68:                                      # word 21: commands only
                    self._run(0x12, 0x00, 4 + i // 4, _CONFIG[i:i + 4])
        cfg = b"".join(self._run(0x02, 0x80, b << 3) for b in range(4))
        if cfg[16:84] != _CONFIG[:68] or cfg[88:] != _CONFIG[72:]:
            raise OSError("atecc608: not Arduino's configuration; not provisioning it")
        if config_open:
            self._run(0x17, 0x00, _crc(cfg, 0, 128))             # lock config, CRC-checked
        if data_open:
            self._run(0x17, 0x81, 0x0000)                        # lock data (as Arduino)
        pub = self._run(0x40, 0x04, _SLOT)                       # GenKey: new private keys
        kx = self._run(0x40, 0x04, _KX_SLOT)
        self._run(0x12, 0x82, _REC << 3, _MAGIC + hashlib.sha256(pub + kx).digest()[:27])

    # -- the chip's command protocol ------------------------------------------------------

    def _run(self, op, p1, p2, data=b"", nonce=None):
        """Wake the chip, run one command (after a Nonce pass-through of ``nonce``, if given),
        send it to idle; the command's answer."""
        self._wake()
        try:
            if nonce is not None:
                self._cmd(0x16, 0x03, 0x0000, nonce)
            return self._cmd(op, p1, p2, data)
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
