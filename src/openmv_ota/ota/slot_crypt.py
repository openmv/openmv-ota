"""The ROMFS slot cipher, as the host sees it.

A board whose boot stub keeps the slots ciphertext at rest (``StubLayout.encrypted``) boots one
slot through the chip's memory cipher engine and leaves the other unmapped; the installer writes
that one, applying the engine's keystream itself (the ``flash_crypt`` C module the firmware
build carries). The key is the chip's own and never leaves it, so the host cannot read or write
a slot's ciphertext -- what it can do, and does here, is name the two inputs the stub and the
installer must agree on for every image:

* the **tweak** -- 80 bits of SHA-256 over a label, the slot index and the SHA-256 of the
  trailer's signed region (the stub's ``common/slot_tweak.c``, pinned to this by a test);
* the **ciphered size** -- how far from the slot's start the cipher reaches: the filesystem
  image and every component, rounded up to the engine's 4 KiB region grain. The pad after it,
  the status sector and the trailer stay in the clear.

The manifest carries both inputs' raw material (``trailer_sha256``, ``crypt_size``), signed, so
the installer knows the keystream before the first body byte arrives (a streamed image delivers
its trailer last). The keystream itself is here only for tests, with a key they choose.
"""

from __future__ import annotations

import hashlib
import struct

from .trailer import Trailer, signed_region

TWEAK_SIZE = 10
CRYPT_GRAIN = 4096                      # the engine's region addresses are 4 KiB aligned
_LABEL = b"omv-slot-crypt-1"            # the stub's slot_label: changing it changes every keystream


def trailer_sha256(t: Trailer) -> bytes:
    """SHA-256 of the trailer's signed region: what the slot tweak is derived from."""
    return hashlib.sha256(signed_region(t)).digest()


def slot_tweak(index: int, trailer_hash: bytes) -> bytes:
    """The tweak slot ``index`` (0 = A, 1 = B) ciphers an image under, from its trailer's
    signed-region hash -- the device derives the same from the manifest's ``trailer_sha256``."""
    if len(trailer_hash) != 32:
        raise ValueError("trailer_sha256 must be 32 bytes")
    h = hashlib.sha256(_LABEL + struct.pack("<I", index) + trailer_hash).digest()
    return h[:TWEAK_SIZE]


def crypt_size(t: Trailer, slot_body_end: int | None = None) -> int:
    """How many bytes from the slot's start the cipher covers for this image: the body and
    every component, rounded up to :data:`CRYPT_GRAIN`, never past the status sector
    (``slot_body_end``, when the caller knows the slot geometry)."""
    used = t.body_size
    for c in t.components:
        used = max(used, c.offset + c.size)
    used = (used + CRYPT_GRAIN - 1) // CRYPT_GRAIN * CRYPT_GRAIN
    if slot_body_end is not None:
        used = min(used, slot_body_end)
    return used


def keystream_xor(key: bytes, tweak: bytes, addr: int, data: bytes) -> bytes:
    """``data`` as the engine stores it at flash address ``addr`` (and back): the AES-128
    stream the stub reproduces in ``mce_stream.c``. For tests, with a key they choose."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(key) != 16 or len(tweak) != TWEAK_SIZE:
        raise ValueError("key must be 16 bytes and the tweak 10")
    out = bytearray(data)
    pos = 0
    while pos < len(out):
        block = (addr >> 4) & 0x0FFFFFFF
        ctr = tweak[:8] + b"\x00\x00" + tweak[8:10] + struct.pack(">I", block)
        ks = Cipher(algorithms.AES(key), modes.CTR(ctr)).encryptor().update(bytes(16))[::-1]
        off = addr & 15
        n = min(16 - off, len(out) - pos)
        for i in range(n):
            out[pos + i] ^= ks[off + i]
        pos += n
        addr += n
    return bytes(out)
