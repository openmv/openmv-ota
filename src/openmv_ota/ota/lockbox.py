"""Per-camera image keys: a release's content key, sealed for keykeeper and wrapped for one camera.

A release is encrypted under one random **content key**. The publisher never sends that key to
the update server in a form the server can read. Instead it **seals** it -- with the account,
and the desk keys that may register this account's cameras -- to **keykeeper**, a small private
service that holds the only private key that opens a seal. When a camera is offered the release,
the server hands keykeeper the seal and the camera's registration record; keykeeper checks the
record's desk signature against the desk keys *inside the seal* (not anything the server says),
and answers with a **lockbox**: the content key, wrapped for that one camera's exchange key. Only
that camera can open it. A server that has been broken into holds seals it can't open and
lockboxes for cameras it isn't; registering a camera of its own needs a desk key it hasn't got.

Two constructions, for two readers:

* **Seal** (publisher -> keykeeper; both ends are full Python): ECDH P-256 with an ephemeral key,
  HKDF-SHA256, AES-256-GCM. JSON, so it can sit in the signed manifest as is.
* **Lockbox** (keykeeper -> camera): ECDH P-256 with an ephemeral key, HKDF-SHA256, then
  AES-256-CBC for the 32-byte key and HMAC-SHA256 over everything before it -- the primitives
  every OpenMV camera already has (``cryptolib`` has no GCM). 145 bytes, fixed::

      ephemeral public key (65) || iv (16) || AES-CBC(k_enc, content key) (32) || HMAC (32)

The camera's half of the lockbox is ``openmv_se.open_lockbox``, frozen into its firmware so
recovery can open one too.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .errors import OtaError

KEY_SIZE = 32
LOCKBOX_SIZE = 65 + 16 + 32 + 32
LOCKBOX_INFO = b"omv-lockbox-v1"
SEAL_INFO = b"omv-seal-v1"
SEAL_VERSION = 1


class LockboxError(OtaError):
    """A seal or lockbox that can't be made or opened, with the reason."""


def _point(hex_or_bytes, what: str) -> ec.EllipticCurvePublicKey:
    raw = bytes.fromhex(hex_or_bytes) if isinstance(hex_or_bytes, str) else bytes(hex_or_bytes)
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        raise LockboxError("%s isn't an uncompressed P-256 public key" % what) from None


def _encoded(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def hkdf(secret: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256 with no salt -- the camera derives the same bytes with its own few lines."""
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(secret)


def _agree(private, peer: ec.EllipticCurvePublicKey) -> bytes:
    """The ECDH secret with ``peer``: ``private`` is a cryptography key, or anything with an
    ``ecdh(peer_point_bytes)`` method (an HSM- or KMS-backed keykeeper key)."""
    if isinstance(private, ec.EllipticCurvePrivateKey):
        return private.exchange(ec.ECDH(), peer)
    return bytes(private.ecdh(_encoded(peer)))


# --- the lockbox: for one camera ------------------------------------------------------------

def make_lockbox(content_key: bytes, exchange_public) -> bytes:
    """``content_key`` wrapped for the camera whose exchange key is ``exchange_public`` (an
    uncompressed P-256 point, hex or bytes)."""
    if len(content_key) != KEY_SIZE:
        raise LockboxError("content key must be %d bytes, got %d" % (KEY_SIZE, len(content_key)))
    peer = _point(exchange_public, "the camera's exchange key")
    eph = ec.generate_private_key(ec.SECP256R1())
    okm = hkdf(eph.exchange(ec.ECDH(), peer), LOCKBOX_INFO, 64)
    iv = secrets.token_bytes(16)
    enc = Cipher(algorithms.AES(okm[:32]), modes.CBC(iv)).encryptor()
    head = _encoded(eph.public_key()) + iv + enc.update(content_key) + enc.finalize()
    return head + hmac.new(okm[32:], head, hashlib.sha256).digest()


def open_lockbox(lockbox: bytes, exchange_private) -> bytes:
    """The content key out of a lockbox, with the camera's exchange key -- the host twin of
    ``openmv_se.open_lockbox`` (tests, and a desk checking what it made)."""
    if len(lockbox) != LOCKBOX_SIZE:
        raise LockboxError("a lockbox is %d bytes, not %d" % (LOCKBOX_SIZE, len(lockbox)))
    okm = hkdf(_agree(exchange_private, _point(lockbox[:65], "the lockbox's key")),
               LOCKBOX_INFO, 64)
    if not hmac.compare_digest(hmac.new(okm[32:], lockbox[:113], hashlib.sha256).digest(),
                               lockbox[113:]):
        raise LockboxError("the lockbox isn't for this camera, or was altered")
    dec = Cipher(algorithms.AES(okm[:32]), modes.CBC(lockbox[65:81])).decryptor()
    return dec.update(lockbox[81:113]) + dec.finalize()


# --- the seal: for keykeeper ----------------------------------------------------------------

def _seal_key(secret: bytes, key_id: int) -> bytes:
    return hkdf(secret, SEAL_INFO + b"\n%d" % key_id, 32)


def seal(contents: dict, keykeeper_public, key_id: int) -> dict:
    """``contents`` (a JSON object) sealed to keykeeper key ``key_id``, as a JSON object."""
    peer = _point(keykeeper_public, "keykeeper's key")
    eph = ec.generate_private_key(ec.SECP256R1())
    nonce = secrets.token_bytes(12)
    eph_pub = _encoded(eph.public_key())
    ct = AESGCM(_seal_key(eph.exchange(ec.ECDH(), peer), key_id)).encrypt(
        nonce, json.dumps(contents, sort_keys=True).encode(), SEAL_INFO + eph_pub)
    return {"v": SEAL_VERSION, "key_id": key_id, "eph": eph_pub.hex(), "nonce": nonce.hex(),
            "ct": ct.hex()}


def seal_key_id(sealed: dict) -> int:
    """Which keykeeper key a seal is for -- so keykeeper picks the right one to open it."""
    try:
        if sealed["v"] != SEAL_VERSION:
            raise LockboxError("seal version %r is not one this software reads" % sealed["v"])
        return int(sealed["key_id"])
    except (KeyError, TypeError, ValueError):
        raise LockboxError("the seal is malformed") from None


def open_seal(sealed: dict, keykeeper_private) -> dict:
    """The contents of a seal, with the keykeeper key it was made for."""
    key_id = seal_key_id(sealed)
    try:
        eph = bytes.fromhex(sealed["eph"])
        nonce = bytes.fromhex(sealed["nonce"])
        ct = bytes.fromhex(sealed["ct"])
    except (KeyError, TypeError, ValueError):
        raise LockboxError("the seal is malformed") from None
    key = _seal_key(_agree(keykeeper_private, _point(eph, "the seal's key")), key_id)
    try:
        plain = AESGCM(key).decrypt(nonce, ct, SEAL_INFO + eph)
    except Exception:
        raise LockboxError("the seal doesn't open with this key, or was altered") from None
    try:
        contents = json.loads(plain)
    except ValueError:
        raise LockboxError("the seal's contents aren't JSON") from None
    if not isinstance(contents, dict):
        raise LockboxError("the seal's contents aren't a JSON object")
    return contents


def seal_release(content_key: bytes, *, account: str, desks: list[dict], keykeeper_public,
                 key_id: int, release: str | None = None) -> dict:
    """A release's content key, sealed to keykeeper with what keykeeper checks a camera
    against: the account, and the desk keys that may register its cameras (``key_id``,
    ``pubkey``, ``expires``, ``revoked``). Keykeeper trusts these, not the server: a desk added
    later registers cameras that later releases (sealed with it) reach."""
    if len(content_key) != KEY_SIZE:
        raise LockboxError("content key must be %d bytes, got %d" % (KEY_SIZE, len(content_key)))
    if not account:
        raise LockboxError("a sealed release needs the account it belongs to")
    if not desks:
        raise LockboxError("a sealed release needs at least one desk key, or no camera could "
                           "ever be given its key")
    contents = {"v": 1, "account": account, "content_key": content_key.hex(),
                "desks": [{"key_id": int(d["key_id"]), "pubkey": str(d["pubkey"]),
                           "expires": str(d["expires"]), "revoked": bool(d.get("revoked"))}
                          for d in desks]}
    if release:
        contents["release"] = release
    return seal(contents, keykeeper_public, key_id)
