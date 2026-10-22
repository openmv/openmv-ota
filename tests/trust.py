"""Shared trust fixtures for the trailer tests: one root, certificates on demand, signed
v4 trailers. A camera embeds only roots; every signing key reaches it as a certificate in
the trailer -- so a test that wants a trusted signer needs a root to certify it with."""

from __future__ import annotations

import hashlib

from openmv_ota.ota import ES256, algorithm_for, keys, sign
from openmv_ota.ota import certificate as C
from openmv_ota.ota import trailer as T

SPEC = algorithm_for(ES256)
ROOT_PRIV = keys.generate_private_key(SPEC)
ROOT_PUB = bytes.fromhex(keys.public_point_hex(ROOT_PRIV.public_key()))
ROOTS = {0: ROOT_PUB}                     # what the firmware stamps as ROOT_KEYS


def root_keys():
    """The committed ``keys/root_keys.json`` shape of :data:`ROOTS`."""
    return [keys.RootKey(0, ES256, ROOT_PUB.hex())]


def pub_bytes(priv) -> bytes:
    return bytes.fromhex(keys.public_point_hex(priv.public_key()))


def certificate(priv, *, kind=C.KIND_OTA, epoch=0, root_id=0, root_priv=ROOT_PRIV) -> bytes:
    """A packed certificate for ``priv``'s public key, signed by ``root_priv``."""
    return C.pack(C.issue(root_priv, root_id, kind, epoch, pub_bytes(priv)))


def trailer(priv, body, *, kind=C.KIND_OTA, epoch=0, root_id=0, root_priv=ROOT_PRIV,
            cert_priv=None, meta=None, product_id=0x1234, payload_version=1 << 24,
            publish_seq=0, body_size=None, components=(), **fields) -> bytes:
    """A packed v4 trailer for ``body``, signed by ``priv``. The certificate is for
    ``cert_priv`` (default ``priv``); pass another key to make a signature that does not
    match its own certificate."""
    t = T.Trailer(
        body_size=len(body) if body_size is None else body_size,
        meta={"k": 1} if meta is None else meta,
        product_id=product_id, payload_version=payload_version, publish_seq=publish_seq,
        body_sha256=hashlib.sha256(body).digest(),
        certificate=certificate(cert_priv or priv, kind=kind, epoch=epoch, root_id=root_id,
                                root_priv=root_priv),
        components=list(components), **fields)
    t.signature = sign.sign_region(priv, T.signed_region(t), SPEC)
    return T.pack_trailer(t)


def verify(alg, pubkey_bytes, sig, msg) -> bool:
    """The device's ``verify`` seam (the mbedtls C module), done with the host's cryptography."""
    spec = algorithm_for(alg)
    try:
        pub = keys.public_key_from_hex(bytes(pubkey_bytes).hex(), spec)
    except Exception:
        return False
    return sign.verify_region(pub, msg, sig, spec)
