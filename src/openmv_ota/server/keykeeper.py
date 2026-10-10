"""The update server's side of keykeeper: asking for a camera's lockbox, and passing the chain of
keykeeper keys on to publishers.

A release encrypted for each camera carries its content key SEALED to keykeeper (the manifest's
``enc.sealed``). To offer it, the server sends keykeeper the seal and the camera's registration
record and gets back a lockbox only that camera opens; the offer carries it. No keykeeper, no
registration, or a refusal: no offer -- an offer without a lockbox is an update the camera can't
open, and it would reject it every poll.

Keykeeper is private (see :mod:`openmv_ota.keykeeper.service`): the server reaches it with a
shared secret, ``OPENMV_OTA_KEYKEEPER_URL`` / ``OPENMV_OTA_KEYKEEPER_TOKEN``.
"""

from __future__ import annotations

import logging

log = logging.getLogger("openmv_ota.server.keykeeper")


class KeykeeperClient:
    """``client`` is an ``httpx.Client``-like object (injected; :func:`build_keykeeper` makes a
    real one)."""

    def __init__(self, url: str, token: str, client, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.token = token
        self.client = client
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": "Bearer " + self.token}

    def lockbox(self, sealed: dict, record: bytes, signature: str) -> str | None:
        """The lockbox (hex) for the camera ``record`` registers, or None if keykeeper refused
        or couldn't be reached (logged: the camera simply isn't offered the release yet)."""
        try:
            r = self.client.post(self.url + "/v1/lockbox", headers=self._headers(),
                                 timeout=self.timeout,
                                 json={"sealed": sealed, "record": record.decode("utf-8"),
                                       "signature": signature})
        except Exception as e:                          # a transport failure: retry next poll
            log.warning("keykeeper unreachable: %r", e)
            return None
        if r.status_code != 200:
            log.warning("keykeeper refused a lockbox (%d): %s", r.status_code, r.text[:200])
            return None
        try:
            return str(r.json()["lockbox"])
        except (ValueError, KeyError, TypeError):
            log.warning("keykeeper answered without a lockbox")
            return None

    def chain(self) -> list | None:
        """The chain of keykeeper keys (public; publishers verify it from their pin)."""
        try:
            r = self.client.get(self.url + "/v1/chain", headers=self._headers(),
                                timeout=self.timeout)
            if r.status_code != 200:
                return None
            chain = r.json()["chain"]
            return chain if isinstance(chain, list) else None
        except Exception as e:
            log.warning("keykeeper chain unavailable: %r", e)
            return None


def build_keykeeper(settings) -> KeykeeperClient | None:
    """The configured keykeeper, or None if this server has none."""
    if not settings.keykeeper_url:
        return None
    import httpx
    return KeykeeperClient(settings.keykeeper_url, settings.keykeeper_token, httpx.Client())


def sealed_of(release: dict | None) -> dict | None:
    """A release's seal, if it is encrypted for each camera (every representation shares it)."""
    for rep in (release or {}).get("representations") or []:
        enc = rep.get("enc") if isinstance(rep, dict) else None
        if isinstance(enc, dict) and isinstance(enc.get("sealed"), dict):
            return enc["sealed"]
    return None
