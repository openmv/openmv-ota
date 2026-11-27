"""OpenMV Live: mint relay capability tokens for cameras at check-in.

The live relay (openmv-cloud ``services/live-relay``) verifies tokens of the form
``exp.hex(hmac_sha256(secret, "role:device_id:exp"))`` -- this module is the minting
side and MUST stay in lockstep with the relay's ``src/auth.ts``.

A device has ONE credential but any number of image **streams** (multi-camera
boards, virtual streams fed from frame buffers): relay rooms are per
``{device}/{stream}``, the token authenticates the device segment only. The
check-in reports its stream names and the grant returns ready-made URLs per
stream (WebSocket push + the deep-sleep ``/poll`` check), so the on-device client
never builds URLs. Tokens renew on every check-in and the TTL outlives a sleep
cycle, so a waking camera always holds a valid credential.

Entitlement seam: :func:`camera_grant` is where per-plan gating lands (does this
account's plan include Live? how many cameras/streams?). Today every registered
device on a deployment that configures the relay gets a grant;
unregistered/bypassed boards never do.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time

# Must stay within the relay's stream-segment charset ([A-Za-z0-9_.-]{1,64}).
_STREAM_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DEFAULT_STREAMS = ("0",)
_MAX_STREAMS = 8                     # a device reporting more is malformed/abusive


def mint_token(secret: str, role: str, device_id: str, ttl_s: int,
               now: float | None = None) -> str:
    exp = int(now if now is not None else time.time()) + ttl_s
    mac = hmac.new(secret.encode(), b"%s:%s:%d" % (role.encode(), device_id.encode(), exp),
                   hashlib.sha256)
    return "%d.%s" % (exp, mac.hexdigest())


def _clean_streams(streams) -> list[str]:
    """The reported stream names that are safe to put in a URL path -- invalid
    names are dropped (not an error: the rest of the grant still works), the
    list is capped, and an empty result falls back to the default stream."""
    good = [s for s in (streams or []) if isinstance(s, str) and _STREAM_NAME.match(s)]
    good = list(dict.fromkeys(good))[:_MAX_STREAMS]        # dedupe, keep order, cap
    return good or list(_DEFAULT_STREAMS)


def viewer_grant(settings, device_id: str, streams=None, datalake_url: str = "",
                 account_id: str = "", product_id=None) -> dict | None:
    """The grant a DASHBOARD needs to read one device: a ``viewer``-role token
    plus ready-made URLs. None when Live is not configured.

    The relay half rides ``token``; when the datalake is configured, its read
    endpoints get their OWN ``viewer`` token under ``datalake`` -- the two
    services sign with separate secrets, deliberately, so they rotate and fail
    independently. Every token is scoped to ONE device and expires in minutes
    (``viewer_token_ttl``, not the day-long device grants: a viewer credential
    leaves the server for a browser or a script and cannot be recalled), so the
    grant is safe to hand out; the signing secrets never leave the server.

    Note the asymmetry with :func:`camera_grant`: a camera's token also covers
    ``/poll`` (role ``camera``), while a viewer may only watch. A viewer token
    can never publish frames or ingest data.

    Each half stands alone: a deployment with a datalake but no relay still
    gets a grant (empty ``token``/``streams``, the datalake half filled), and
    vice versa. None only when neither read side is configured."""
    relay_on = bool(settings.live_relay_url and settings.live_token_secret)
    lake_on = bool(datalake_url and settings.datalake_token_secret)
    if not (relay_on or lake_on):
        return None
    grant: dict = {"token": "", "streams": {}, "expires_in_s": settings.viewer_token_ttl}
    if relay_on:
        token = mint_token(settings.live_token_secret, "viewer", device_id,
                           settings.viewer_token_ttl)
        base = settings.live_relay_url.rstrip("/")
        ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        grant["token"] = token
        grant["streams"] = {
            s: {"watch_url": "%s/watch/%s/%s?token=%s" % (ws_base, device_id, s, token)}
            for s in _clean_streams(streams)
        }
    if lake_on:
        # The read side: topics for the pane list, logs/{topic} for backscroll --
        # bearer-authed with the DATALAKE's own viewer token, not the relay's. The ACCOUNT
        # and the PRODUCT are in the token and the URL, as the camera's data was filed: a
        # camera that changes owner, or is moved to another product, keeps its id, and a
        # grant for less read back through what it sent before -- to a token limited to
        # the product it is in now, the product it was in. ``product_id`` None is every
        # product of the account ("*", which no product id can be).
        from .datalog import account_key
        dl = datalake_url.rstrip("/")
        account = account_key(account_id)
        product = "*" if product_id is None else "%s" % product_id   # as ingest_grant files it
        base = "%s/api/v1/devices/%s/%s/%s" % (dl, account, product, device_id)
        grant["datalake"] = {
            # (`device/` first: a product's viewer token is "{account}/{product}" and a
            # camera's ingest token "{account}/{product}/{device}"; this one is neither)
            "token": mint_token(settings.datalake_token_secret, "viewer",
                                "device/%s/%s/%s" % (account, product, device_id),
                                settings.viewer_token_ttl),
            "topics_url": base + "/topics",
            "logs_url": base + "/logs",                     # + /{topic}
            "series_url": base + "/series",                 # + /{topic}
            "expires_in_s": settings.viewer_token_ttl,
        }
    return grant


def camera_grant(settings, device_id: str, streams=None) -> dict | None:
    """The ``live`` object for a check-in response, or None when Live is not
    configured (no relay URL / no secret) -- the response simply omits the key."""
    if not (settings.live_relay_url and settings.live_token_secret):
        return None
    token = mint_token(settings.live_token_secret, "camera", device_id,
                       settings.live_token_ttl)
    base = settings.live_relay_url.rstrip("/")
    ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    per_stream = {
        s: {
            "camera_url": "%s/camera/%s/%s?token=%s" % (ws_base, device_id, s, token),
            "poll_url": "%s/poll/%s/%s?token=%s" % (base, device_id, s, token),
        }
        for s in _clean_streams(streams)
    }
    return {"streams": per_stream, "expires_in_s": settings.live_token_ttl}
