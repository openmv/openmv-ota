"""The per-IP fixed-window rate limiter."""

from __future__ import annotations

from openmv_ota.server.ratelimit import RateLimiter, ipv6_prefix


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


def test_a_flood_inside_one_window_cannot_grow_the_table():
    # nothing is stale, so the sweep frees nothing: the quietest half goes, the loud stay
    clk = _Clock()
    rl = RateLimiter(5, now=clk, max_tracked=4)
    for _ in range(3):
        rl.allow("steady")
    for ip in ("f1", "f2", "f3", "f4", "f5", "f6"):
        rl.allow(ip)
        assert len(rl._hits) <= 4
    assert "steady" in rl._hits                  # a real client keeps its count


def test_ipv6_prefix():
    assert ipv6_prefix("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == "2001:db8:1:2::/64"
    assert ipv6_prefix("[2001:db8:1:2::1]:443") == "2001:db8:1:2::/64"
    assert ipv6_prefix("fe80::1%eth0") == "fe80::/64"
    assert ipv6_prefix("[2001:db8::1") == "2001:db8::/64"            # unterminated bracket
    assert ipv6_prefix("203.0.113.7") is None                        # IPv4: per address only
    assert ipv6_prefix("::ffff:203.0.113.7") is None                 # IPv4-mapped: likewise
    assert ipv6_prefix("-") is None and ipv6_prefix("garbage") is None


def test_rotating_through_a_slash_64_hits_the_prefix_ceiling():
    clk = _Clock()
    rl = RateLimiter(2, per_prefix_per_minute=5, now=clk)
    # every address is fresh, so the per-address tier alone would allow all of these
    results = [rl.allow("2001:db8:1:2::%x" % i) for i in range(8)]
    assert results == [True] * 5 + [False] * 3
    assert rl.allow("2001:db8:1:3::1") is True   # another /64 has its own ceiling
    assert rl.allow("203.0.113.7") is True       # IPv4 is untouched by the /64 tier
    clk.t += 60
    assert rl.allow("2001:db8:1:2::99") is True  # the ceiling rolls over like any window


def test_each_ipv6_device_keeps_its_own_per_address_budget():
    clk = _Clock()
    rl = RateLimiter(2, per_prefix_per_minute=100, now=clk)
    assert rl.allow("2001:db8::a") and rl.allow("2001:db8::a")
    assert rl.allow("2001:db8::a") is False      # this device is over its own limit...
    assert rl.allow("2001:db8::b") is True       # ...its neighbour on the same /64 is not


def test_prefix_tier_off_by_default():
    rl = RateLimiter(1_000)
    assert all(rl.allow("2001:db8::%x" % i) for i in range(2_000))
