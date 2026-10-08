"""``openmv_ota.se.soft`` -- the camera's own keys, on a board with no secure element.

The same interface as the chip modules, with the keys kept by the camera itself: two P-256
private keys -- the identity key (signs) and the exchange key (ECDH) -- in the board's key area,
the end of its boot partition (``key_store``). The keys are made here the first time the camera
opens it, from the hardware RNG (``os.urandom``), and written once; nothing ever erases them, and
updating the firmware or the romfs leaves them where they are. All the elliptic-curve work is
mbedtls's, through ``ecdsa_verify`` -- nothing here does curve arithmetic.

The key area holds two record slots of 128 bytes. A record is ``OMVK`` 01, 11 zero bytes, the
identity key, the exchange key, then the first 16 bytes of the SHA-256 of all that (96 bytes;
the rest of the slot stays blank). The first valid slot is the camera's keys. A blank slot is
where they are made; a slot that is neither -- a write cut off by a power loss -- is skipped,
since flash can't be rewritten without an erase, and the keys are made in the next one.

Until the device is locked down, any code on the camera can read these keys (and a debugger
can read the flash). The lockdown's read protection covers the key area where it already is.

RAM BUDGET: this module runs inside your application, so its memory is your memory. Opening reads
the 256-byte key area once; each operation takes 160 bytes of RNG output for mbedtls.
"""

import hashlib
import os

import ecdsa_verify
import key_store

_MAGIC = b"OMVK\x01"
_SLOT = 128                       # bytes per record slot in the key area
_REC = 96                         # bytes of a record (three 32-byte flash words)
_ENTROPY = 160                    # RNG bytes per mbedtls call (nonce + blinding, with margin)


def _der_int(v):
    v = bytes(v).lstrip(b"\x00") or b"\x00"
    if v[0] & 0x80:
        v = b"\x00" + v
    return b"\x02" + bytes([len(v)]) + v


def _der_sig(rs):
    """A raw ``r || s`` signature (64 bytes) as DER ``SEQUENCE { INTEGER r, INTEGER s }``."""
    body = _der_int(rs[:32]) + _der_int(rs[32:64])
    return b"\x30" + bytes([len(body)]) + body


def _new_key():
    """32 RNG bytes that are a valid P-256 private key (mbedtls checks 1 <= d < n; a miss is
    about 1 in 2**32, and is simply drawn again)."""
    while True:
        d = os.urandom(32)
        try:
            ecdsa_verify.public_key(d, os.urandom(_ENTROPY))
            return d
        except ValueError:
            pass


def _valid(rec):
    return rec[:5] == _MAGIC and hashlib.sha256(rec[:80]).digest()[:16] == rec[80:_REC]


class SecureElement:
    """This camera's own keys, made on first open. Raises OSError if the key area holds no
    usable record and has no blank slot left. See :mod:`openmv_ota.se` for the interface."""

    def __init__(self):
        area = key_store.read()                        # ram-ok: the fixed 256-byte key area
        for off in range(0, key_store.SIZE, _SLOT):
            rec = area[off:off + _REC]
            if _valid(rec):
                break
            if rec == b"\xff" * _REC:                      # blank: the keys are made here
                rec = _MAGIC + bytes(11) + _new_key() + _new_key()
                rec += hashlib.sha256(rec).digest()[:16]
                key_store.write(off, rec)
                if key_store.read()[off:off + _REC] != rec:     # ram-ok: 256 bytes
                    raise OSError("key store: the keys did not read back")
                break
        else:
            raise OSError("key store: no usable key record and no blank slot")
        self._id = rec[16:48]
        self._kx = rec[48:80]
        self._pub = self._kx_pub = None

    def public_key(self):
        """The identity key's public half: 65 bytes, ``04 || X || Y``."""
        if self._pub is None:
            self._pub = ecdsa_verify.public_key(self._id, os.urandom(_ENTROPY))
        return self._pub

    def certificate(self):
        """None: these keys carry no maker's certificate."""
        return None

    def sign(self, digest):
        """An ECDSA P-256 signature, DER-encoded, over the 32-byte SHA-256 ``digest``."""
        if len(digest) != 32:
            raise ValueError("digest must be 32 bytes")
        return _der_sig(ecdsa_verify.sign(self._id, digest, os.urandom(_ENTROPY)))

    def random(self, n):
        """``n`` random bytes from the hardware RNG."""
        return os.urandom(n)

    def ecdh_public_key(self):
        """The exchange key's public half: 65 bytes, ``04 || X || Y``."""
        if self._kx_pub is None:
            self._kx_pub = ecdsa_verify.public_key(self._kx, os.urandom(_ENTROPY))
        return self._kx_pub

    def ecdh(self, peer):
        """The 32-byte ECDH secret of the exchange key and ``peer`` (65 bytes, ``04 || X || Y``)."""
        return ecdsa_verify.ecdh(self._kx, peer, os.urandom(_ENTROPY))
