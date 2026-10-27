"""Signing-key certificates -- how a camera comes to trust the key that signed an image.

A camera (its boot stub and its firmware) embeds only **root** public keys: a primary and a
backup, fixed when the stub is flashed over USB. Every key that signs an image -- the project's
everyday signing key, a factory key, a desk key -- is introduced by a *certificate*: a small
fixed record naming the key, what it may sign and an epoch, signed by a root. The trailer
carries the certificate, so verifying an image is two signatures: the root over the
certificate, then the certified key over the image. New signing keys need no change on the
camera; retiring one is a higher epoch, which the camera remembers and refuses to go below.

Layout (little-endian, 160 bytes)::

    magic(4s)=OMVC version kind root_id epoch alg(int32) pubkey(65s) pad(7) signature(64s)
    \\____________________ signed region: bytes 0..95 ____________________/

``kind`` says what the key may sign: :data:`KIND_OTA` for releases, :data:`KIND_FACTORY` for
factory images -- the one kind whose first boot opens the camera's key-provisioning window,
which is why it is a certified property and not a convention on key ids. ``root_id`` names the
embedded root that signed the certificate. ``epoch`` moves only when a key is retired; a camera
that has confirmed a release at epoch N refuses certificates below N from then on.

Certificates carry **P-256 keys only** (``alg`` = ES256, a 65-byte uncompressed point, a 64-byte
root signature): the stub's verifier is p256-m, and one curve keeps the format fixed-size. The
wider curves in :mod:`.algorithms` remain for manifests, not for certified keys.

This module is the format's source of truth and mirrors the device's parser (``boot.py``);
packing and parsing are pure, :func:`issue` and :func:`verify` do the host-side crypto.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from .algorithms import ES256, algorithm_for
from .errors import OtaError

MAGIC = b"OMVC"
VERSION = 1
SIZE = 160
SIGNED_SIZE = 96          # magic .. pubkey+pad; the root signs exactly these bytes

KIND_OTA = 0              # signs releases
KIND_FACTORY = 1          # signs factory images (whose first boot provisions the camera's keys)
_KINDS = (KIND_OTA, KIND_FACTORY)

_STRUCT = "<4sIIIIi65s7x64s"       # == SIZE bytes; pinned by a test

_SPEC = algorithm_for(ES256)


@dataclass
class Certificate:
    """A decoded certificate. ``pubkey`` is the certified key's uncompressed P-256 point
    (``04 || X || Y``); ``signature`` the root's raw ``R || S`` over the signed region."""

    kind: int
    root_id: int
    epoch: int
    pubkey: bytes
    alg: int = ES256
    signature: bytes = b""

    @property
    def key_id(self) -> int:
        """The certified key's id, derived from the key itself (see :func:`key_id_of`)."""
        return key_id_of(self.pubkey)


def key_id_of(pubkey: bytes) -> int:
    """A key's id: the first 31 bits of its public point's SHA-256, never 0. Derived rather than
    assigned so two projects (or an account's desk keys) can never collide, and so a certificate
    needs no id field of its own -- the same rule desk keys already use."""
    return int.from_bytes(hashlib.sha256(bytes(pubkey)).digest()[:4], "big") & 0x7FFFFFFF or 1


def _check(c: Certificate) -> None:
    if c.alg != ES256:
        raise OtaError("certificates carry P-256 keys only (alg %d is not ES256)" % c.alg)
    if c.kind not in _KINDS:
        raise OtaError("unknown certificate kind %d" % c.kind)
    if len(c.pubkey) != _SPEC.pubkey_size or c.pubkey[0] != 0x04:
        raise OtaError("pubkey must be a %d-byte uncompressed P-256 point" % _SPEC.pubkey_size)


def signed_region(data) -> bytes:
    """The bytes the root signs: given a :class:`Certificate`, pack them; given raw certificate
    bytes, the first :data:`SIGNED_SIZE` of them."""
    if isinstance(data, Certificate):
        _check(data)
        return struct.pack(_STRUCT, MAGIC, VERSION, data.kind, data.root_id, data.epoch,
                           data.alg, data.pubkey, b"\x00" * _SPEC.sig_size)[:SIGNED_SIZE]
    if len(data) < SIGNED_SIZE:
        raise OtaError("certificate too small: %d < %d bytes" % (len(data), SIGNED_SIZE))
    return bytes(data[:SIGNED_SIZE])


def pack(c: Certificate) -> bytes:
    """Serialise a signed certificate (``signature`` must be set)."""
    region = signed_region(c)
    if len(c.signature) != _SPEC.sig_size:
        raise OtaError("certificate signature must be %d bytes, got %d"
                       % (_SPEC.sig_size, len(c.signature)))
    return region + c.signature


def parse(data) -> Certificate:
    """Decode and structurally validate a certificate (no crypto): magic, version, kind, a P-256
    key. Raises ``OtaError`` on any malformation. The fields are trustworthy only after
    :func:`verify` checks the root's signature."""
    if len(data) < SIZE:
        raise OtaError("certificate too small: %d < %d bytes" % (len(data), SIZE))
    magic, version, kind, root_id, epoch, alg, pubkey, signature = struct.unpack_from(
        _STRUCT, data, 0)
    if magic != MAGIC:
        raise OtaError("bad certificate magic %r (expected %r)" % (magic, MAGIC))
    if version != VERSION:
        raise OtaError("unsupported certificate version %d (this codec reads %d)"
                       % (version, VERSION))
    c = Certificate(kind, root_id, epoch, bytes(pubkey), alg, bytes(signature))
    _check(c)
    return c


def issue(root_private_key, root_id: int, kind: int, epoch: int, pubkey: bytes) -> Certificate:
    """Certify ``pubkey`` (an uncompressed P-256 point) with a root's private key. This is the
    offline ceremony: the root never signs anything else."""
    from .sign import sign_region

    c = Certificate(kind, root_id, epoch, bytes(pubkey))
    c.signature = sign_region(root_private_key, signed_region(c), _SPEC)
    return c


def verify(data, roots) -> Certificate:
    """Parse ``data`` and check the root's signature against ``roots`` (``{root_id: pubkey
    bytes}``, the set a camera embeds). Returns the trusted :class:`Certificate`; raises
    ``OtaError`` for an unknown root or a signature that does not verify."""
    from .keys import public_key_from_hex
    from .sign import verify_region

    c = parse(data)
    root = roots.get(c.root_id)
    if root is None:
        raise OtaError("certificate names unknown root %d" % c.root_id)
    pub = public_key_from_hex(bytes(root).hex(), _SPEC)
    if not verify_region(pub, signed_region(data), c.signature, _SPEC):
        raise OtaError("certificate signature does not verify against root %d" % c.root_id)
    return c
