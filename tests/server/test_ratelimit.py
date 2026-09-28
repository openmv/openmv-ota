"""The per-IP fixed-window rate limiter."""

from __future__ import annotations

import pytest

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


# --- the shared (metadata-store) limiter ----------------------------------------------------
def _store():
    from openmv_ota.server.metastore import SqliteMetadataStore

    s = SqliteMetadataStore(":memory:")
    s.migrate()
    return s


def test_two_instances_sharing_a_store_enforce_one_limit():
    """The whole point: N server instances used to allow N times the rate."""
    from openmv_ota.server.ratelimit import SharedRateLimiter

    store, clock = _store(), [1000.0]
    a = SharedRateLimiter(store, 5, now=lambda: clock[0])
    b = SharedRateLimiter(store, 5, now=lambda: clock[0])
    got = [(a if i % 2 else b).allow("10.0.0.1") for i in range(8)]
    assert got == [True] * 5 + [False] * 3
    assert a.allow("10.0.0.2")                               # a different address is its own
    clock[0] += 60                                           # next window: counts start over
    assert b.allow("10.0.0.1")
    left = store.query_one("SELECT COUNT(*) AS n FROM rate_hits WHERE window_start < ?",
                           (int(clock[0]) // 60 * 60 - 60,))["n"]
    assert left == 0                                         # swept to two windows


def test_the_ipv6_prefix_is_counted_first_and_stops_there():
    from openmv_ota.server.ratelimit import SharedRateLimiter

    store = _store()
    lim = SharedRateLimiter(store, 100, per_prefix_per_minute=3, now=lambda: 600.0)
    got = [lim.allow("2001:db8:1:2::%x" % i) for i in range(6)]    # rotating through one /64
    assert got == [True] * 3 + [False] * 3
    rows = store.query_one("SELECT COUNT(*) AS n FROM rate_hits WHERE key NOT LIKE 'net:%'")["n"]
    assert rows == 3, "an over-budget prefix must not mint address rows"


def test_disabled_and_fail_open_to_an_in_process_floor():
    from openmv_ota.server.ratelimit import SharedRateLimiter

    assert SharedRateLimiter(object(), 0).allow("x")         # 0 = disabled, store untouched

    class _Down:
        def rate_sweep(self, before):
            raise OSError("database is down")

    lim = SharedRateLimiter(_Down(), 2, now=lambda: 60.0)
    assert [lim.allow("10.0.0.9") for _ in range(3)] == [True, True, False]


def test_the_backend_setting_picks_the_limiter():
    from openmv_ota.server.app import _rate_limiter
    from openmv_ota.server.metastore import PostgresMetadataStore
    from openmv_ota.server.ratelimit import RateLimiter, SharedRateLimiter
    from openmv_ota.server.settings import ServerSettings

    lite = _store()
    pg = PostgresMetadataStore("postgresql://x", connect=lambda: _store()._conn)
    for backend, store, kind in (("auto", lite, RateLimiter), ("auto", pg, SharedRateLimiter),
                                 ("memory", pg, RateLimiter), ("shared", lite, SharedRateLimiter)):
        assert type(_rate_limiter(ServerSettings(checkin_rate_backend=backend), store)) is kind
    with pytest.raises(ValueError, match="checkin_rate_backend"):
        _rate_limiter(ServerSettings(checkin_rate_backend="redis"), lite)
