"""Stateless capability tokens for the artifact gateway.

An HMAC token binds a release to an expiry so ``GET /d/{token}/…`` can be authorized with no DB
lookup, and **one token guards the whole bundle** (the manifest *and* its relative image/delta
siblings resolve under the same ``/d/{token}/`` prefix). Signed with a server secret; only minted
for a registered device that's been offered a release.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


def _b64e(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _b64d(data: bytes) -> bytes:
    return base64.urlsafe_b64decode(data + b"=" * (-len(data) % 4))


def _sig(secret: str, body: bytes) -> bytes:
    return _b64e(hmac.new(secret.encode(), body, hashlib.sha256).digest())


def mint(secret: str, release_id: str, *, ttl: int = 3600, now=time.time) -> str:
    """A capability token authorizing ``release_id``'s artifacts for ``ttl`` seconds."""
    body = _b64e(json.dumps({"r": release_id, "exp": int(now()) + ttl},
                            separators=(",", ":")).encode())
    return (body + b"." + _sig(secret, body)).decode()


def verify(secret: str, token: str, *, now=time.time) -> str | None:
    """The ``release_id`` a token authorizes, or ``None`` if malformed/tampered/expired."""
    try:
        body, sig = token.encode().split(b".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sig(secret, body)):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(now()):
        return None
    return payload.get("r")
