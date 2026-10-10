"""``openmv_ota.se.soft`` -- the camera's own keys, on a board with no secure element.

The same interface as the chip modules, with the keys kept by the camera itself: P-256 private
keys -- the identity key (signs) and the exchange key (ECDH) -- in the board's key area at the end
of its boot partition (``key_store``). They are made once, at a desk -- by ``flash`` or
registration, through :meth:`SecureElement.provision` -- from the hardware RNG, and never
rewritten: firmware and romfs updates don't touch the boot partition. Opening only reads; a camera
in the field either has its keys or fails, and never makes new ones on its own. All
elliptic-curve work is mbedtls's, through ``ecdsa_verify``.

THE KEY AREA is 4 KB: sixteen 256-byte slots, filled from the top of the area down (so the area
could later be made smaller without losing the slots in use). Each slot is one record or blank. A
record is eight 32-byte words -- 32 bytes is the largest flash write unit here (the H7's), and
fields are aligned to their size:

    word 0   description: "OMVK", version 1, protection (0 plain, 1 sealed by the chip), key
             count, a type byte per key (1 identity, 2 exchange); every other byte blank
    word 1-5 the keys, 32 bytes each, in the order of their types; unused words blank
    word 6   sealed records only: GCM IV (bytes 0-11) and tag (bytes 16-31); else blank

On the N6 the key area is in the external NOR, which can be read off the board: there the keys
are SEALED -- encrypted with AES-256-GCM under the chip's own hardware key (``key_store.seal``,
the DHUK in SAES), with the description as authenticated data -- so the NOR opens nothing on its
own or on another chip. Everywhere else the key area is inside the chip and the keys are plain.
    word 7   check: SHA-256 of words 0-6 exactly as stored -- WRITTEN LAST, it commits the record

Words that hold nothing are never programmed (an H7 flash word can be programmed once), and the
check covers whatever the memory reads there. Opening a slot, by its check word:

    blank            a write that never got this far: its keys were never used -- skipped
    matches          a complete record: the newest one is the camera's keys
    anything else    damaged (or cut off during that final write) -- an error, never new keys

A complete record of a version or key type this firmware doesn't know is an error too ("newer
firmware"), never skipped: an older firmware must not mistake a newer identity for none. The
check is always SHA-256 over words 0-6, in every version, for the same reason.

A power cut while an H7 flash word is being programmed can leave it with an error its ECC can't
correct, and reading such a word is a bus fault. ``key_store`` reads it as EIO instead, and an
unreadable word counts like any other cut-off write: before the check, the slot is skipped; in the
check, or under a check that can't be verified, the record is damaged.

Until the device is locked down, any code on the camera can read these keys (and a debugger can
read the flash). The lockdown's read protection covers the key area where it already is.

RAM BUDGET: this module runs inside your application, so its memory is your memory. Opening reads
one 256-byte slot at a time; each operation takes 160 bytes of RNG output for mbedtls.
"""

import hashlib
import os

import ecdsa_verify
import key_store

_SLOT = 256                       # bytes per slot: one record
_WORD = 32                        # the record's unit: the largest flash write unit
_KEYS = 5                         # key words in a record
_MAGIC = b"OMVK"
_VERSION = 1
_PLAIN = 0                        # description protection byte: keys stored as they are
_SEALED = 1                       # ...or sealed by the chip (key_store.seal: the N6's DHUK)
_IDENTITY, _EXCHANGE = 1, 2       # description key-type bytes
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


class NotProvisioned(OSError):
    """The camera has no keys: it was never provisioned, or its key area was wiped."""


def _words(off):
    """The slot at ``off`` as its eight words, None for a word the flash can't read: on the H7, a
    flash word a power cut left with an error its ECC can't correct reads as EIO."""
    try:
        slot = key_store.read(off, _SLOT)
        return [slot[i:i + _WORD] for i in range(0, _SLOT, _WORD)]
    except OSError:
        words = []
        for i in range(0, _SLOT, _WORD):
            try:
                words.append(key_store.read(off + i, _WORD))
            except OSError:
                words.append(None)
        return words


def _scan():
    """The newest complete record and the offset of the next blank slot (None if full). Raises
    if the newest record that wasn't cut off before its check is damaged -- or can't be read."""
    blank = bytes([key_store.BLANK]) * _WORD
    newest = free = None
    damaged = False
    for i in range(key_store.SIZE // _SLOT):           # top down: the order they're written in
        off = key_store.SIZE - (i + 1) * _SLOT
        words = _words(off)
        if all(w == blank for w in words):
            free = off                                 # written in order: nothing past here
            break
        check = words[7]
        if check == blank:
            continue                                   # cut off before its check: never used
        damaged = None in words                        # an unreadable check, or keys under it
        if not damaged:
            slot = b"".join(words)
            damaged = check != hashlib.sha256(slot[:7 * _WORD]).digest()
        if not damaged:
            newest = slot
    if damaged:
        raise OSError("key store: the camera's newest key record is damaged; re-provision it "
                      "at a desk")
    return newest, free


def _keys(rec):
    """The record's keys by type, checked to be a record this firmware understands, and
    unsealed by the chip if it was sealed."""
    desc = rec[:_WORD]
    n = desc[6]
    types = desc[8:8 + n]
    sealed = desc[5] == _SEALED and hasattr(key_store, "unseal")
    if (desc[:4] != _MAGIC or desc[4] != _VERSION or not (desc[5] == _PLAIN or sealed)
            or not 0 < n <= _KEYS or len(set(types)) != n
            or any(t not in (_IDENTITY, _EXCHANGE) for t in types)):
        raise OSError("key store: the camera's key record is from newer firmware")
    keys = rec[_WORD:(1 + n) * _WORD]
    if sealed:
        crypto = rec[6 * _WORD:7 * _WORD]
        try:
            keys = key_store.unseal(desc, crypto[:12], keys, crypto[16:])
        except OSError:
            raise OSError("key store: the chip can't unseal the camera's keys (its hardware key "
                          "is unavailable or changed); re-provision it at a desk") from None
    return {t: keys[i * _WORD:(i + 1) * _WORD] for i, t in enumerate(types)}


class SecureElement:
    """This camera's own keys. Raises :class:`NotProvisioned` if it has none, OSError if its
    newest record is damaged or from newer firmware. See :mod:`openmv_ota.se` for the
    interface."""

    def __init__(self):
        rec, _ = _scan()
        if rec is None:
            raise NotProvisioned("key store: this camera has no keys; provision it")
        keys = _keys(rec)
        self._id = keys.get(_IDENTITY)
        self._kx = keys.get(_EXCHANGE)
        self._pub = self._kx_pub = None

    @classmethod
    def provision(cls):
        """``(keys, made)``: make the camera's keys if it has none -- the identity and the exchange
        key, in the next blank slot -- then open them. ``made`` is False if it already had keys.
        Raises, writing nothing, if the newest record is damaged or from newer firmware, or the
        key area is full. Called at a desk only (``openmv-ota flash``, registration)."""
        rec, free = _scan()
        if rec is not None:
            return cls(), False
        if free is None:
            raise OSError("key store: no blank slot left in the key area")
        b = bytes([key_store.BLANK])
        sealed = hasattr(key_store, "seal")
        types = bytes([_IDENTITY, _EXCHANGE])
        desc = _MAGIC + bytes([_VERSION, _SEALED if sealed else _PLAIN, len(types)]) + b + types
        desc += b * (_WORD - len(desc))
        keys = _new_key() + _new_key()
        crypto = b * _WORD                                   # word 6: blank unless sealed
        if sealed:
            iv = os.urandom(12)
            out = key_store.seal(desc, iv, keys)
            keys, crypto = out[:-16], iv + b * 4 + out[-16:]
        head = desc + keys                                   # words 0-2
        key_store.write(free, head)
        if sealed:
            key_store.write(free + 6 * _WORD, crypto)
        body = head + b * (6 * _WORD - len(head)) + crypto   # as the memory will read it
        key_store.write(free + 7 * _WORD, hashlib.sha256(body).digest())   # last: commits it
        return cls(), True

    def public_key(self):
        """The identity key's public half: 65 bytes, ``04 || X || Y``."""
        if self._pub is None:
            self._pub = ecdsa_verify.public_key(self._key(self._id), os.urandom(_ENTROPY))
        return self._pub

    def certificate(self):
        """None: these keys carry no maker's certificate."""
        return None

    def sign(self, digest):
        """An ECDSA P-256 signature, DER-encoded, over the 32-byte SHA-256 ``digest``."""
        if len(digest) != 32:
            raise ValueError("digest must be 32 bytes")
        return _der_sig(ecdsa_verify.sign(self._key(self._id), digest, os.urandom(_ENTROPY)))

    def random(self, n):
        """``n`` random bytes from the hardware RNG."""
        return os.urandom(n)

    def ecdh_public_key(self):
        """The exchange key's public half: 65 bytes, ``04 || X || Y``."""
        if self._kx_pub is None:
            self._kx_pub = ecdsa_verify.public_key(self._key(self._kx), os.urandom(_ENTROPY))
        return self._kx_pub

    def ecdh(self, peer):
        """The 32-byte ECDH secret of the exchange key and ``peer`` (65 bytes, ``04 || X || Y``)."""
        return ecdsa_verify.ecdh(self._key(self._kx), peer, os.urandom(_ENTROPY))

    @staticmethod
    def _key(key):
        """One key, one job: a record without a key of the type asked for has none to lend."""
        if key is None:
            raise OSError("key store: the camera's record has no key for that job")
        return key
