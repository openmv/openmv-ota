"""OpenMV datalake: mint ingest grants for devices at check-in.

The datalake (openmv-cloud ``services/datalake``) verifies ``ingest`` tokens
whose subject is ``"{account}/{device}"`` -- the account rides INSIDE the MAC so
a device can't attribute its data to another account. This module is the minting
side; it shares Live's token FORMAT (:func:`openmv_ota.server.live.mint_token`)
but signs with the datalake's OWN secret -- the two integrations are deliberately
decoupled, rotating and failing independently -- and MUST stay in lockstep with
the datalake's ``tokens.py``.

The grant hands the device a ready-made ingest base URL (it appends the topic,
e.g. ``console``) plus the token, so the on-device client builds no URLs. Like
the live grant it renews every check-in and outlives a sleep cycle. Registered
devices only; unregistered/bypassed boards never get one (same lever as OTA and
Live).
"""

from __future__ import annotations

from .live import mint_token

# The account URL segment when a device has no explicit account (self-host's ''
# sentinel). The datalake requires a non-empty, path-safe account.
_DEFAULT_ACCOUNT = "default"


def ingest_grant(settings, account_id: str, device_id: str) -> dict | None:
    """The ``ingest`` object for a check-in response, or None when the datalake
    is not configured (no URL / no secret) -- the response omits the key."""
    if not (settings.datalake_url and settings.datalake_token_secret):
        return None
    account = account_id or _DEFAULT_ACCOUNT
    token = mint_token(settings.datalake_token_secret, "ingest",
                       "%s/%s" % (account, device_id), settings.datalake_token_ttl)
    base = settings.datalake_url.rstrip("/")
    return {
        "url": "%s/api/v1/ingest/%s/%s" % (base, account, device_id),  # + /{topic}
        "token": token,
        "expires_in_s": settings.datalake_token_ttl,
    }
