"""The rollout decision: stable/monotonic staged-%, the gates, and auto-pause."""

from __future__ import annotations

import pytest

from openmv_ota.server.rollout import (
    fallback_payload_version, offers_update, ramp_action, settled, should_autopause,
    staged_in, validate_stages,
)


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


def _slots(running_pending, running_confirmed, n=2):
    out = [{"slot": "A", "running": True, "payload_version": 2, "counter": 4,
            "pending": running_pending, "confirmed": running_confirmed}]
    if n == 2:
        out.append({"slot": "B", "running": False, "payload_version": 1, "counter": 3,
                    "pending": True, "confirmed": True})
    return out


def test_settled_holds_an_update_back_while_a_trial_is_unproven():
    """The slot an install writes is the one holding the last proven release. Updating during a
    trial trades a known-good fallback for an unproven one, at the moment the device has already
    said it is unsure of itself. Waiting ends as soon as it confirms."""
    assert settled(_slots(running_pending=True, running_confirmed=False)) is False
    assert settled(_slots(running_pending=True, running_confirmed=True)) is True   # settled
    assert settled(_slots(running_pending=False, running_confirmed=False)) is True  # not a trial


@pytest.mark.parametrize("slots", [
    None, [],                                            # v1 payload / didn't say
    _slots(True, False, n=1),                            # single-image: no fallback to protect
    [{"slot": "A", "running": False}, {"slot": "B", "running": False}],   # nothing running
])
def test_settled_treats_unknown_as_ok_to_offer(slots):
    """This gate protects a fallback. A device that has none, or that never told us, must not be
    held back by it -- the DEVICE is the authoritative check either way."""
    assert settled(slots) is True


def test_offers_update_defers_to_a_mid_trial_device():
    base = dict(current_payload_version=1, release_payload_version=2, rollout_state="active",
                rollout_percent=100, rollout_id="r", device_id="d")
    assert offers_update(**base, slots=_slots(True, True)) is True
    assert offers_update(**base, slots=_slots(True, False)) is False    # mid-trial: wait


def test_fallback_payload_version_reports_the_other_slot():
    assert fallback_payload_version(_slots(True, True)) == 1
    assert fallback_payload_version(_slots(True, True, n=1)) is None    # no other slot
    assert fallback_payload_version(None) is None                       # didn't say
    # a blank second slot is "unknown", not "version 0" -- never render that to an operator
    assert fallback_payload_version(
        [{"slot": "A", "running": True, "payload_version": 2},
         {"slot": "B", "running": False, "payload_version": 0}]) is None


def test_offers_update_allow_downgrade():
    # test-only hook: an older/equal release is offered ONLY with allow_downgrade set, and even
    # then only when the other gates pass (active + staged). It never bypasses those.
    base = dict(current_payload_version=2, release_payload_version=1, rollout_state="active",
                rollout_percent=100, rollout_id="r", device_id="d")
    assert offers_update(**base) is False                                     # gated off by default
    assert offers_update(**base, allow_downgrade=True) is True                # downgrade offered
    assert offers_update(**{**base, "release_payload_version": 2}, allow_downgrade=True) is True
    assert offers_update(**{**base, "rollout_state": "paused"}, allow_downgrade=True) is False
    assert offers_update(**{**base, "rollout_percent": 0}, allow_downgrade=True) is False


def test_should_autopause():
    assert should_autopause(6, 100, 0.05) is True
    assert should_autopause(4, 100, 0.05) is False
    assert should_autopause(0, 0, 0.05) is False         # no attempts yet


def test_running_body_sha256_reads_the_running_slot():
    from openmv_ota.server.rollout import running_body_sha256
    slots = [{"slot": "B", "running": True, "body_sha256": "aa" * 32},
             {"slot": "A", "running": False, "body_sha256": "bb" * 32}]
    assert running_body_sha256(slots) == "aa" * 32
    assert running_body_sha256(None) is None                      # no slots at all
    assert running_body_sha256([]) is None
    assert running_body_sha256([{"slot": "A", "running": False}]) is None  # nothing running
    # "" from the device (a trailer that would not parse) is unknown-to-us, same as absent
    assert running_body_sha256([{"slot": "A", "running": True, "body_sha256": ""}]) is None
    assert running_body_sha256([{"slot": "A", "running": True}]) is None


# --- rollout ramps ---------------------------------------------------------------------------

def test_validate_stages_cleans_and_defaults():
    out = validate_stages([{"percent": 1}, {"percent": 10, "min_soak": 3600, "min_attempted": 50,
                                            "max_failure_rate": 0.02}, {"percent": 100}])
    assert out[0] == {"percent": 1.0, "min_soak": 0.0, "min_attempted": 0}   # optional fields default
    assert out[1]["max_failure_rate"] == 0.02
    assert [s["percent"] for s in out] == [1.0, 10.0, 100.0]


@pytest.mark.parametrize("stages, msg", [
    ([], "non-empty"),
    ("nope", "non-empty"),
    ([{"min_soak": 1}], "numeric percent"),
    ([{"percent": 101}], "0..100"),
    ([{"percent": 10}, {"percent": 5}], "must not decrease"),
    ([{"percent": 1, "min_soak": -1}], ">= 0"),
    ([{"percent": 1, "max_failure_rate": 2}], "0..1"),
    ([5], "must be an object"),
])
def test_validate_stages_rejects(stages, msg):
    with pytest.raises(ValueError, match=msg):
        validate_stages(stages)


def test_ramp_action_holds_until_soak_and_attempted_met():
    stages = [{"percent": 1.0, "min_soak": 3600, "min_attempted": 50}, {"percent": 10.0}]
    assert ramp_action(stages, 0, 10, 0, 100, 0.05) == ("hold", None)          # too soon, too few
    assert ramp_action(stages, 0, 50, 0, 3599, 0.05) == ("hold", None)         # attempted ok, soak not
    assert ramp_action(stages, 0, 49, 0, 3600, 0.05) == ("hold", None)         # soak ok, attempted not
    assert ramp_action(stages, 0, 50, 0, 3600, 0.05) == ("raise", 10.0)        # both met -> raise


def test_ramp_action_pause_beats_raise():
    stages = [{"percent": 1.0, "min_soak": 0, "min_attempted": 10, "max_failure_rate": 0.05},
              {"percent": 10.0}]
    # gates for a raise are all met, but the stage's failure rate is over its ceiling -> pause wins
    assert ramp_action(stages, 0, 100, 20, 10_000, 0.05) == ("pause", None)


def test_ramp_action_uses_default_ceiling_and_stops_at_last_stage():
    stages = [{"percent": 1.0, "min_soak": 0, "min_attempted": 0}]              # no max_failure_rate
    assert ramp_action(stages, 0, 100, 6, 10, 0.05) == ("pause", None)          # 6% > default 5%
    assert ramp_action(stages, 0, 100, 4, 10, 0.05) == ("hold", None)           # last stage: never raises
    assert ramp_action(stages, 0, 0, 0, 10, 0.05) == ("hold", None)             # no attempts: no rate
    assert ramp_action([], 0, 0, 0, 0, 0.05) == ("hold", None)                  # not a ramp
    assert ramp_action(stages, 5, 0, 0, 0, 0.05) == ("hold", None)              # index out of range
