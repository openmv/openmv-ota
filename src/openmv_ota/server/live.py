"""OpenMV Live: mint relay capability tokens for cameras at check-in.

The live relay (openmv-cloud ``services/live-relay``) verifies tokens of the form
``exp.hex(hmac_sha256(secret, "role:device_id:exp"))`` -- this module is the minting
side and MUST stay in lockstep with the relay's ``src/auth.ts``.

The camera gets ready-made URLs (WebSocket stream + the deep-sleep ``/poll`` check)
so the on-device client never builds URLs. Tokens renew on every check-in and the
TTL outlives a sleep cycle, so a waking camera always holds a valid credential.

Entitlement seam: :func:`camera_grant` is where per-plan gating lands (does this
account's plan include Live? how many cameras?). Today every registered device on
a deployment that configures the relay gets a token; unregistered/bypassed boards
never do.
"""

from __future__ import annotations

import hashlib
import hmac
import time


def mint_token(secret: str, role: str, device_id: str, ttl_s: int,
               now: float | None = None) -> str:
    exp = int(now if now is not None else time.time()) + ttl_s
    mac = hmac.new(secret.encode(), b"%s:%s:%d" % (role.encode(), device_id.encode(), exp),
                   hashlib.sha256)
    return "%d.%s" % (exp, mac.hexdigest())


def camera_grant(settings, device_id: str) -> dict | None:
    """The ``live`` object for a check-in response, or None when Live is not
    configured (no relay URL / no secret) -- the response simply omits the key."""
    if not (settings.live_relay_url and settings.live_token_secret):
        return None
    token = mint_token(settings.live_token_secret, "camera", device_id,
                       settings.live_token_ttl)
    base = settings.live_relay_url.rstrip("/")
    ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return {
        "camera_url": "%s/camera/%s?token=%s" % (ws_base, device_id, token),
        "poll_url": "%s/poll/%s?token=%s" % (base, device_id, token),
        "expires_in_s": settings.live_token_ttl,
    }
