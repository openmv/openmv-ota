"""A tiny per-IP fixed-window rate limiter (in-memory, per worker).

Keyed by **IP** (bounded by real clients) -- never by the attacker-controlled ``device_id``, which
would itself be an unbounded-growth vector. Approximate under multiple workers (each has its own
window); good enough as the check-in edge limiter in front of the registration call.

Two tiers, because an IPv6 caller is handed a whole /64 (2**64 addresses):

- **per address** (``per_minute``): unchanged for everyone. An IPv4 NAT fleet shares one address
  exactly as before, and each IPv6 device keeps a bucket of its own.
- **per IPv6 /64** (``per_prefix_per_minute``): a higher ceiling on everything one /64 sends
  together, so rotating through the prefix cannot turn the per-address limit into nothing.
  Collapsing IPv6 to its /64 outright (what the cloud website does) would instead squeeze every
  device at an IPv6 site into one per-address bucket -- a power-on storm at a factory would be
  throttled at its 61st camera -- so the /64 gets its own, larger budget.

The table is bounded at ``max_tracked`` entries: past it, rolled-over windows are swept, and when
that frees nothing (a flood of distinct addresses inside one window) the quietest half is evicted.
Deliberately the quietest, not the oldest: a flood is a crowd of one-hit keys, while a steady client
keeps its count -- and evicting by age would hand a patient grinder a fresh bucket every time the
table fills.
"""

from __future__ import annotations

import ipaddress
import time


def ipv6_prefix(ip: str) -> str | None:
    """The /64 an IPv6 address belongs to (``"2001:db8:1:2::/64"``), or None for IPv4, an
    IPv4-mapped IPv6 address, or anything that does not parse (which is then limited per key only).
    Tolerates ``[addr]:port`` and a ``%zone`` suffix."""
    s = ip.strip()
    if s.startswith("["):
        s = s[1:s.find("]")] if "]" in s else s[1:]
    s = s.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(s)
    except ValueError:
        return None
    if addr.version != 6 or addr.ipv4_mapped is not None:
        return None
    return str(ipaddress.ip_network((addr, 64), strict=False))


class RateLimiter:
    def __init__(self, per_minute: int, *, per_prefix_per_minute: int = 0, now=time.monotonic,
                 max_tracked: int = 100_000):
        self._max = per_minute
        self._prefix_max = per_prefix_per_minute
        self._now = now
        self._max_tracked = max_tracked
        self._hits: dict[str, tuple[float, int]] = {}

    def _bound(self, t: float) -> None:
        if len(self._hits) < self._max_tracked:
            return
        self._hits = {k: (s, c) for k, (s, c) in self._hits.items() if t - s < 60.0}
        if len(self._hits) >= self._max_tracked:         # nothing was stale: keep the loudest half
            keep = sorted(self._hits.items(), key=lambda kv: kv[1][1], reverse=True)
            self._hits = dict(keep[:self._max_tracked // 2])

    def _count(self, key: str, t: float) -> int:
        start, count = self._hits.get(key, (t, 0))
        if t - start >= 60.0:                            # window rolled over
            start, count = t, 0
        self._hits[key] = (start, count + 1)
        return count + 1

    def allow(self, ip: str) -> bool:
        if self._max <= 0:
            return True                                  # disabled
        t = self._now()
        self._bound(t)
        ok = self._count(ip, t) <= self._max
        prefix = ipv6_prefix(ip) if self._prefix_max > 0 else None
        if prefix is not None:
            # counted even when the address itself is over: the /64 total is what rotation spends
            ok = (self._count("net:" + prefix, t) <= self._prefix_max) and ok
        return ok
