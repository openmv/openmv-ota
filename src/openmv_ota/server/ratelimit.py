"""A tiny per-IP fixed-window rate limiter (in-memory, per worker).

Keyed by **IP** (bounded by real clients) -- never by the attacker-controlled ``device_id``, which
would itself be an unbounded-growth vector. Approximate under multiple workers (each has its own
window); good enough as the check-in edge limiter in front of the registration call.
"""

from __future__ import annotations

import time


class RateLimiter:
    def __init__(self, per_minute: int, *, now=time.monotonic):
        self._max = per_minute
        self._now = now
        self._hits: dict[str, tuple[float, int]] = {}

    def allow(self, ip: str) -> bool:
        if self._max <= 0:
            return True                                  # disabled
        t = self._now()
        start, count = self._hits.get(ip, (t, 0))
        if t - start >= 60.0:                            # window rolled over
            start, count = t, 0
        self._hits[ip] = (start, count + 1)
        return count + 1 <= self._max
