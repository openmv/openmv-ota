"""The rollout decision: stable/monotonic staged-%, the gates, and auto-pause."""

from __future__ import annotations

from openmv_ota.server.rollout import offers_update, should_autopause, staged_in


def test_staged_in_bounds():
    assert staged_in("r", "d", 100) is True
    assert staged_in("r", "d", 150) is True
    assert staged_in("r", "d", 0) is False
    assert staged_in("r", "d", -5) is False


def test_staged_in_deterministic_and_monotonic():
    assert staged_in("r", "dev", 50) == staged_in("r", "dev", 50)
    for i in range(50):                                  # a staged device stays staged as % rises
        dev = "dev%d" % i
        if staged_in("r", dev, 10):
            assert staged_in("r", dev, 50) and staged_in("r", dev, 100)


def test_staged_in_distribution_tracks_percent():
    n = 2000
    staged = sum(staged_in("roll1", "dev%d" % i, 25) for i in range(n))
    assert 0.20 * n < staged < 0.30 * n                  # ~25%


def test_staged_in_salt_differs_by_rollout():
    a = [staged_in("rollA", "dev%d" % i, 50) for i in range(200)]
    b = [staged_in("rollB", "dev%d" % i, 50) for i in range(200)]
    assert a != b                                        # not the same units every rollout


def test_offers_update_gates():
    base = dict(current_payload_version=1, release_payload_version=2, rollout_state="active",
                rollout_percent=100, rollout_id="r", device_id="d")
    assert offers_update(**base) is True
    assert offers_update(**{**base, "rollout_state": "paused"}) is False
    assert offers_update(**{**base, "release_payload_version": 1}) is False   # equal
    assert offers_update(**{**base, "release_payload_version": 0}) is False   # older
    assert offers_update(**{**base, "rollout_percent": 0}) is False           # not staged


def test_should_autopause():
    assert should_autopause(6, 100, 0.05) is True
    assert should_autopause(4, 100, 0.05) is False
    assert should_autopause(0, 0, 0.05) is False         # no attempts yet
