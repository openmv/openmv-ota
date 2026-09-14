"""OpenMV datalake: mint ingest grants for devices at check-in.

The datalake (openmv-cloud ``services/datalake``) verifies ``ingest`` tokens
whose subject is ``"{account}/{product}/{device}"`` -- the account and the product
ride INSIDE the MAC, so a device can neither attribute its data to another account
nor file it under another product (the product is what lets the datalake answer
for a whole product's devices at once). This module is the minting
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


def ingest_grant(settings, account_id: str, device_id: str, product_id) -> dict | None:
    """The ``ingest`` object for a check-in response, or None when the datalake
    is not configured (no URL / no secret) -- the response omits the key."""
    if not (settings.datalake_url and settings.datalake_token_secret):
        return None
    account = account_id or _DEFAULT_ACCOUNT
    token = mint_token(settings.datalake_token_secret, "ingest",
                       "%s/%s/%s" % (account, product_id, device_id), settings.datalake_token_ttl)
    base = settings.datalake_url.rstrip("/")
    return {
        "url": "%s/api/v1/ingest/%s/%s/%s" % (base, account, product_id, device_id),  # + /{topic}
        "token": token,
        "expires_in_s": settings.datalake_token_ttl,
    }


def product_grant(settings, account_id: str, product_id) -> dict | None:
    """A dashboard's read credential for ONE PRODUCT's data across all its devices:
    the datalake's product ``viewer`` token (subject ``"{account}/{product}"``) plus
    the two URLs it opens. None when the datalake is not configured. Lives
    ``viewer_token_ttl`` like a device's viewer grant -- it leaves the server."""
    if not (settings.datalake_url and settings.datalake_token_secret):
        return None
    account = account_id or _DEFAULT_ACCOUNT
    base = settings.datalake_url.rstrip("/")
    return {
        "datalake": {
            "token": mint_token(settings.datalake_token_secret, "viewer",
                                "%s/%s" % (account, product_id), settings.viewer_token_ttl),
            "topics_url": "%s/api/v1/products/%s/%s/topics" % (base, account, product_id),
            "series_url": "%s/api/v1/products/%s/%s/series" % (base, account, product_id),  # + /{topic}
            "expires_in_s": settings.viewer_token_ttl,
        },
        "expires_in_s": settings.viewer_token_ttl,
    }
