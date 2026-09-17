"""Payload confidentiality: the artifact a camera downloads is ciphertext.

Images are signed, which says who made them; it does not say who may read them.
An account id is not a secret (the organization page prints it), so anyone who
knows one can ask the server for the release it is offering and read the firmware
out of it. This module is what closes that: the bytes leaving the build are
encrypted, the bytes in the artifact store are encrypted, and the only place the
plaintext exists is inside a camera that was built with the key.

What that is worth, stated plainly: it makes **a dump of a physical board** the
cheapest way in. The key is a frozen constant in the firmware, so an attacker who
can read a board's flash has it -- and because one firmware image serves a whole
board type, that one dump covers that board type's fleet. Fuses and readout
protection are what change that, and they are a different project. Everything
cheaper than a dump -- guessing an account id, scraping a URL, a leaked bucket, a
copy of a backup -- gets ciphertext.

Two keys, for two jobs:

* a **board key**, minted per project per board target and baked into that board's
  firmware. It only ever unwraps; it never touches bulk data.
* a **content key**, random per artifact, which does the encrypting. It travels in
  the manifest wrapped under the board key, and the manifest is signed, so the
  wrap cannot be swapped for one an attacker knows.

An artifact carries **one wrap per live board key**, not one wrap. A board can only
install what its own baked constants can unwrap, and firmware is how a new board key
reaches a camera -- so a rotation that published under the new key alone would be
undeliverable to exactly the fleet that still needs updating. Publishing under both
lets the new key roll out as firmware, at the fleet's pace, and the old wrap is
dropped from later releases once nothing is left running the old constant. 48 bytes
per generation.

Per board target rather than per project because firmware and images are already
built per board: it costs nothing and means a dumped H7 does not hand over the N6
fleet. NOT per device -- a device holds no per-device secret, and one would buy
nothing here anyway: whoever dumped a board can simply let that board fetch and
decrypt its own updates.

**AES-256-CBC**, because it is the mode every networked OpenMV board already has
(``cryptolib`` ships with SSL; CTR is compiled out upstream, and turning it on
would mean new firmware everywhere before a single encrypted release could
install). Its streaming shape suits the installer, which decrypts in a fixed
window and keeps the chain across a resumed download.

Framing avoids PKCS#7 entirely: the plaintext is zero-padded to the block size and
the real length is recorded in the signed manifest, so the device stops at the
byte count it was told rather than trusting a padding byte it just decrypted.
Integrity is NOT CBC's job here -- the manifest is signed, and the device verifies
the reconstructed image's digest and then its trailer's signature before booting
it. Nothing acts on a decrypted byte before that.
"""

from __future__ import annotations

import hashlib
import secrets

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .errors import OtaError

ALG = "aes-256-cbc"
KEY_SIZE = 32
BLOCK = 16
WRAP_SIZE = BLOCK + KEY_SIZE      # iv || the two enciphered key blocks


def new_key() -> bytes:
    """A fresh 256-bit key. Used for both roles -- a board key and a content key
    are the same kind of secret, minted in different places."""
    return secrets.token_bytes(KEY_SIZE)


def _cipher(key: bytes, iv: bytes):
    if len(key) != KEY_SIZE:
        raise OtaError("payload key must be %d bytes, got %d" % (KEY_SIZE, len(key)))
    if len(iv) != BLOCK:
        raise OtaError("payload iv must be %d bytes, got %d" % (BLOCK, len(iv)))
    return Cipher(algorithms.AES(key), modes.CBC(iv))


def wrap_key(board_key: bytes, content_key: bytes) -> bytes:
    """``iv || AES-CBC(board_key, content_key)`` -- 48 bytes, no padding (a key is
    exactly two blocks). Rides in the signed manifest."""
    if len(content_key) != KEY_SIZE:
        raise OtaError("content key must be %d bytes, got %d" % (KEY_SIZE, len(content_key)))
    iv = secrets.token_bytes(BLOCK)
    enc = _cipher(board_key, iv).encryptor()
    return iv + enc.update(content_key) + enc.finalize()


def unwrap_key(board_key: bytes, wrap: bytes) -> bytes:
    """The content key out of a wrap. The host side of what the device does with
    the constant its firmware was built with."""
    if len(wrap) != WRAP_SIZE:
        raise OtaError("payload key wrap must be %d bytes, got %d" % (WRAP_SIZE, len(wrap)))
    dec = _cipher(board_key, bytes(wrap[:BLOCK])).decryptor()
    return dec.update(bytes(wrap[BLOCK:])) + dec.finalize()


def encrypt(plaintext: bytes, key: bytes, iv: bytes | None = None) -> tuple[bytes, bytes]:
    """``(iv, ciphertext)`` for one artifact. The plaintext is zero-padded to the
    block size; the caller records ``len(plaintext)`` in the manifest, which is how
    the device knows where the artifact really ends.

    ``iv`` is generated unless one is given. Passing one is for re-encrypting bytes a
    SIGNED manifest already describes -- the HIL rig's tamper scenarios do exactly that
    -- and never for a new artifact, which gets a fresh iv and a fresh content key."""
    if iv is None:
        iv = secrets.token_bytes(BLOCK)
    pad = -len(plaintext) % BLOCK
    enc = _cipher(key, iv).encryptor()
    return iv, enc.update(bytes(plaintext) + b"\0" * pad) + enc.finalize()


def decrypt(ciphertext: bytes, key: bytes, iv: bytes, size: int) -> bytes:
    """The artifact back, truncated to the ``size`` the manifest declared.

    The host needs this too: a delta is built against the previous release, and
    the release the server holds is ciphertext."""
    if len(ciphertext) % BLOCK:
        raise OtaError("ciphertext is not a whole number of blocks (%d)" % len(ciphertext))
    if not 0 <= size <= len(ciphertext):
        raise OtaError("declared payload size %d is not inside %d bytes of ciphertext"
                       % (size, len(ciphertext)))
    dec = _cipher(key, iv).decryptor()
    return (dec.update(bytes(ciphertext)) + dec.finalize())[:size]


def encrypt_artifact(plaintext: bytes, board_keys: dict[int, bytes]) -> tuple[bytes, dict]:
    """``(ciphertext, enc)`` for one representation: a fresh content key, wrapped
    under every live board key, and the ``enc`` block that goes in the signed
    manifest. Keyed by key id as a STRING, because the block is JSON.

    ``sha256`` is the CIPHERTEXT's digest. It is what the update server checks at
    publish (it cannot read the plaintext, and should not be able to), and it is
    the only digest anything but a camera can check. The plaintext digest is still
    the top-level ``sha256`` the device verifies after decrypting."""
    if not board_keys:
        raise OtaError("no board key to encrypt for")
    content_key = new_key()
    iv, ciphertext = encrypt(plaintext, content_key)
    return ciphertext, {
        "alg": ALG,
        "wraps": {str(kid): wrap_key(key, content_key).hex()
                  for kid, key in sorted(board_keys.items())},
        "iv": iv.hex(),
        "size": len(plaintext),
        "sha256": hashlib.sha256(ciphertext).hexdigest(),
    }


def select_wrap(wraps: dict, board_keys: dict[int, bytes]) -> tuple[int, bytes]:
    """``(key_id, wrap)`` for the newest key held by both sides.

    Newest rather than first: a camera that has been updated should use the key it
    was updated to, so the day the old wrap stops being published is a day nothing
    notices."""
    for kid in sorted(board_keys, reverse=True):
        wrap = wraps.get(str(kid))
        if wrap is not None:
            return kid, bytes.fromhex(wrap)
    raise OtaError("this artifact is not encrypted for any key we hold (have %s, offered %s)"
                   % (sorted(board_keys), sorted(wraps)))


def decrypt_artifact(ciphertext: bytes, board_keys: dict[int, bytes], enc: dict) -> bytes:
    """The plaintext of an artifact described by a manifest's ``enc`` block."""
    if enc.get("alg") != ALG:
        raise OtaError("unsupported payload encryption %r" % (enc.get("alg"),))
    try:
        wraps = enc["wraps"]
        iv = bytes.fromhex(enc["iv"])
        size = int(enc["size"])
    except (KeyError, TypeError, ValueError) as e:
        raise OtaError("malformed payload encryption block: %s" % e) from None
    key_id, wrap = select_wrap(wraps, board_keys)
    return decrypt(ciphertext, unwrap_key(board_keys[key_id], wrap), iv, size)
