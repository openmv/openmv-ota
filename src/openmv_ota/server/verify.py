"""Validate a device against the central openmv-swd-ids registration registry.

The device check-in is gated on this call: an unregistered ``(board, id)`` gets nothing and
leaves zero footprint. We call swd-ids' server-to-server ``POST /api/v1/registration/verify`` (token-authed).

Two defenses against an attacker looping the id-space:
* **positive-only caching** -- a registered result is cached (bounded by the real fleet); a
  negative is *never* cached (an unbounded negative cache would be the very exhaustion we're
  defending against). Rate-limiting the check-in edge is the caller's job (before ``verify``).
* **fail-closed** -- any error/timeout/non-200 from swd-ids is treated as *not registered*, so an
  outage can't be leveraged into serving.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Registration:
    registered: bool
    board_type: str = ""
    owner_ref: str = ""


_NO = Registration(False)


class RegistrationVerifier:
    """``client`` is an ``httpx.Client``-like object (injected; ``build_verifier`` supplies a real
    one). ``now`` is a monotonic clock seam for the cache TTL."""

    def __init__(self, url: str, token: str, client, *, cache_ttl: float = 300.0,
                 timeout: float = 5.0, now=time.monotonic):
        self._url = url
        self._token = token
        self._client = client
        self._ttl = cache_ttl
        self._timeout = timeout
        self._now = now
        self._cache: dict[tuple[str, str], tuple[float, Registration]] = {}

    def verify(self, board: str, device_id: str) -> Registration:
        key = (board, device_id)
        hit = self._cache.get(key)
        if hit is not None and hit[0] > self._now():
            return hit[1]
        reg = self._call(board, device_id)
        if reg.registered:                          # cache positives only
            self._cache[key] = (self._now() + self._ttl, reg)
        return reg

    def _call(self, board: str, device_id: str) -> Registration:
        try:
            resp = self._client.post(
                self._url, json={"board": board, "id": device_id},
                headers={"Authorization": "Bearer " + self._token}, timeout=self._timeout)
        except Exception:
            return _NO                              # fail-closed on any transport error
        if resp.status_code != 200:
            return _NO
        try:
            data = resp.json()
        except Exception:
            return _NO
        if not data.get("registered"):
            return _NO
        return Registration(True, board_type=data.get("board_type", "") or "",
                            owner_ref=data.get("owner_ref", "") or "")


def build_verifier(settings) -> RegistrationVerifier:
    import httpx
    return RegistrationVerifier(settings.swd_ids_verify_url, settings.swd_ids_verify_token,
                                httpx.Client())
