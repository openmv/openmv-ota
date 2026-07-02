"""The per-IP fixed-window rate limiter."""

from __future__ import annotations

from openmv_ota.server.ratelimit import RateLimiter


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_disabled_when_zero():
    rl = RateLimiter(0)
    assert all(rl.allow("ip") for _ in range(1000))


def test_limits_per_window_and_rolls_over():
    clk = _Clock()
    rl = RateLimiter(2, now=clk)
    assert rl.allow("a") and rl.allow("a")       # 2 allowed
    assert rl.allow("a") is False                # 3rd blocked
    assert rl.allow("b") is True                 # a different IP has its own budget
    clk.t += 60                                  # window rolls over
    assert rl.allow("a") is True


def test_stale_ips_are_swept_to_bound_memory():
    clk = _Clock()
    rl = RateLimiter(5, now=clk, max_tracked=3)
    for ip in ("a", "b", "c"):                   # fill the table with within-window entries
        rl.allow(ip)
    clk.t += 61                                  # all three windows now stale
    rl.allow("d")                                # crossing max_tracked triggers a sweep
    assert set(rl._hits) == {"d"}                # stale a/b/c evicted, only the live one kept
