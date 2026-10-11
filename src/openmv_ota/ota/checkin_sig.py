"""Signed check-ins: a camera proving it is the camera its registration names.

A camera signs each check-in with its identity key once its clock is trusted (``openmv_se.
checkin_signature`` on the device), and sends the result in one header::

    X-OMV-Signature: v=1;ts=1791720000;sig=3045...

``sig`` is a DER ECDSA P-256 signature over::

    SHA-256("omv-checkin-v1\\n" + ts + "\\n" + body)

with ``ts`` the camera's Unix time in decimal and ``body`` the exact bytes it POSTed. The time
keeps a recorded check-in from being replayed later: the server takes one only within
:data:`WINDOW_S` of its own clock. The key it checks against is the identity key in the camera's
registration record -- which a desk signed, so a broken-into server can't swap it.
"""
from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

from .errors import OtaError

HEADER = "x-omv-signature"
CONTEXT = b"omv-checkin-v1\n"
WINDOW_S = 300                                    # a camera's clock vs the server's, either way
_MAX_HEADER = 512                                 # v, ts and a 72-byte DER signature fit in 200


class CheckinSignatureError(OtaError):
    """A check-in signature that doesn't verify, with the reason (short: it is counted and shown)."""


def digest(body: bytes, ts: int) -> bytes:
    """What the camera signs: the context, its time and the body, hashed."""
    return hashlib.sha256(CONTEXT + b"%d\n" % ts + body).digest()


def parse(header: str) -> tuple[int, bytes]:
    """``(ts, der_signature)`` from the header's value, or raise saying what is wrong with it."""
    if len(header) > _MAX_HEADER:
        raise CheckinSignatureError("malformed")
    fields = {}
    for part in header.split(";"):
        k, sep, v = part.strip().partition("=")
        if not sep or k in fields:
            raise CheckinSignatureError("malformed")
        fields[k] = v
    if fields.get("v") != "1":
        raise CheckinSignatureError("unknown version")
    try:
        ts = int(fields["ts"])
        sig = bytes.fromhex(fields["sig"])
    except (KeyError, ValueError):
        raise CheckinSignatureError("malformed") from None
    if not sig:
        raise CheckinSignatureError("malformed")
    return ts, sig


def verify(header: str, body: bytes, identity: str, now: float) -> int:
    """Check a check-in's signature header against the camera's registered identity key (an
    uncompressed P-256 point, hex). Returns the camera's time; raises
    :class:`CheckinSignatureError` (reason ``"stale"``, ``"bad signature"``, ...) otherwise."""
    ts, sig = parse(header)
    if abs(now - ts) > WINDOW_S:
        raise CheckinSignatureError("stale")
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bytes.fromhex(identity))
        key.verify(sig, digest(body, ts), ec.ECDSA(Prehashed(hashes.SHA256())))
    except (ValueError, InvalidSignature):
        raise CheckinSignatureError("bad signature") from None
    return ts
