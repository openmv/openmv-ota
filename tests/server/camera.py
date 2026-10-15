"""Check in as a registered camera does: signed by the identity key its registration names.

The server gives a check-in the full treatment -- a fleet row, telemetry, Live and ingest grants
-- only when that key signed it; an unsigned one gets update offers only. A test of the full
treatment checks in through :func:`check`, which records the camera's registration the first
time (what ``flash factory`` leaves on the server, minus the desk's signature) and signs every
body it posts.
"""

from __future__ import annotations

import hashlib
import json
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from openmv_ota.ota import checkin_sig

_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551   # P-256's n


def identity_of(body: dict) -> str:
    """The id the server keeps a check-in under: board-qualified when the body names a board."""
    board = body.get("board")
    return "%s:%s" % (board, body["device_id"]) if board else body["device_id"]


def key_for(device_id: str) -> ec.EllipticCurvePrivateKey:
    """``device_id``'s identity key: the same one every time, so a camera that checks in twice
    signs twice with the key its registration names."""
    scalar = int.from_bytes(hashlib.sha256(device_id.encode()).digest(), "big") % (_ORDER - 1)
    return ec.derive_private_key(scalar + 1, ec.SECP256R1())


def point(key) -> str:
    """A key's public half as the registration stores it: an uncompressed P-256 point, hex."""
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint).hex()


def register(store, device_id: str, account_id: str = "", *, key=None):
    """Record ``device_id``'s registration to ``account_id`` in ``store``, naming ``key`` (by
    default :func:`key_for` the id) as its identity key. Returns the key."""
    key = key if key is not None else key_for(device_id)
    store.put_registration(device_id, account_id, record=b"{}", signature="00",
                           identity=point(key), exchange=point(key), desk_key_id=1,
                           issued="2026-10-10T12:00:00Z", registered_by="desk")
    return key


def sign(key, body: bytes, ts: int | None = None) -> str:
    """The ``X-OMV-Signature`` header for ``body``, signed by ``key`` at ``ts`` (default now)."""
    ts = int(time.time()) if ts is None else ts
    sig = key.sign(checkin_sig.digest(body, ts), ec.ECDSA(Prehashed(hashes.SHA256())))
    return "v=1;ts=%d;sig=%s" % (ts, sig.hex())


def post(client, body: bytes, signature: str | None = None):
    """POST raw check-in bytes, with ``signature`` as the header when there is one."""
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-OMV-Signature"] = signature
    return client.post("/api/v1/check", content=body, headers=headers)


def check(client, body: dict, *, key=None, registered_to: str | None = None):
    """POST ``body`` to ``/api/v1/check`` signed, as a registered camera. A camera with no
    registration yet is registered first: to ``registered_to``, or else to the account the body
    reports. Signed by ``key``, or by :func:`key_for` the camera. Returns the response."""
    device_id = identity_of(body)
    key = key if key is not None else key_for(device_id)
    store = client.app.state.metastore
    if store.get_registration(device_id) is None:
        register(store, device_id,
                 (body.get("account_id") or "") if registered_to is None else registered_to,
                 key=key)
    raw = json.dumps(body).encode()
    return post(client, raw, sign(key, raw))
