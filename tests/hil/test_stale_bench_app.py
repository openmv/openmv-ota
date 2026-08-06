"""verify_golden must catch the board running the PREVIOUS scenario's bench app.

Every scenario's golden is version 1.0.0, so the payload check cannot tell them apart. When a
golden flash silently does not take, the board keeps the old app and the run measures the wrong
thing until it times out. That is the N6 `watchdog_bite` flakiness: it always follows `watchdog`,
whose app differs only in whether it stops feeding, so the stale app looks entirely healthy on the
UART while wdt.bit/wdt.stop can never arrive.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ci" / "hil"))

import ota_cycle  # noqa: E402


def _cap(lines):
    ota_cycle._CAP = types.SimpleNamespace(raw=list(lines))
    ota_cycle._FLASH_MARK = 0


GOLDEN = "boot: mounted FRONT (payload 16777216)"


def test_the_wrong_scenario_app_is_caught(monkeypatch):
    _cap([GOLDEN, "app: scenario watchdog", "app: device_id abc123"])
    with pytest.raises(RuntimeError, match="watchdog_bite"):
        ota_cycle.verify_golden_uart("OPENMV_N6", budget=1, want_app="watchdog_bite")


def test_the_right_app_passes():
    _cap([GOLDEN, "app: scenario watchdog_bite", "app: device_id abc123"])
    assert ota_cycle.verify_golden_uart(
        "OPENMV_N6", budget=5, want_app="watchdog_bite") == "abc123"


def test_no_expectation_means_no_check():
    """Callers that do not care (or older boards with no tag) must be unaffected."""
    _cap([GOLDEN, "app: scenario watchdog", "app: device_id abc123"])
    assert ota_cycle.verify_golden_uart("OPENMV_N6", budget=5) == "abc123"


def test_an_untagged_app_does_not_trip_it():
    """A board whose app predates the tag has no `app: scenario` line -- do not fail it blind."""
    _cap([GOLDEN, "app: device_id abc123"])
    assert ota_cycle.verify_golden_uart(
        "OPENMV_N6", budget=5, want_app="watchdog_bite") == "abc123"


def test_the_bench_app_emits_the_tag():
    """The guard is worthless if the app never says which scenario it is."""
    src = ota_cycle.bench_main_py("OPENMV_N6", "lan", "watchdog_bite")
    assert "app: scenario watchdog_bite" in src


def test_the_reinstall_scenario_starts_from_a_PROMOTED_board():
    """The only scenario whose second phase begins somewhere other than golden.

    Every other scenario starts on the factory image, so "what happens when an update fails AFTER
    you have already taken one" was unreachable -- the run ends when the first cycle settles. In
    the field that state is the normal one, and the only thing behind the promoted image is the
    FACTORY golden: a failed install there does not cost you the update, it costs you every update
    ever taken.
    """
    spec = ota_cycle.SCENARIOS["reinstall"]
    assert spec["end"] == "promoted", "phase 1 must actually promote, or phase 2 proves nothing"
    then = spec["then"]
    assert then["publish"] == "corrupt_sha", "phase 2 must fail the INTEGRITY gate, not the transport"
    assert then["end"] == "golden", "and must record that it falls back to the factory image"
    assert "install.reject_sha" in then["expect"], "the sha256 gate firing is the point"
    assert "confirm.promoted" in then["forbid"], "a bad image must never promote"


def test_reinstall_is_in_the_stable_boards_suite_and_dispatchable():
    """Useless if it never runs. It rides with the other negative paths on the stable boards."""
    for board in ("OPENMV_N6", "OPENMV_RT1060"):
        assert "reinstall" in ota_cycle.regression_scenarios(board, ota_cycle.BOARDS[board]["network"])
    wf = (Path(__file__).resolve().parents[2] / ".github/workflows/hil-ota.yml").read_text()
    assert "reinstall]" in wf, "must be selectable from workflow_dispatch"


def test_every_scenario_with_a_second_phase_declares_what_it_needs():
    """A `then` block is run by the same machinery as phase 1, so it must carry the same fields."""
    for name, spec in ota_cycle.SCENARIOS.items():
        then = spec.get("then")
        if then is None:
            continue
        for field in ("desc", "publish", "version", "end", "expect"):
            assert field in then, "%s.then is missing %s" % (name, field)


def test_reinstall_phase2_is_scored_on_markers_not_slot_size():
    """Phase 2 must not pass or fail on how big the board's flash slot is.

    Its assertion is that the integrity gate fires from a NON-golden start and the bad image never
    promotes -- which the marker set states exactly. Additionally waiting for the device to settle
    back on golden requires the retry-exhaust fallback, and that is slot-sized: each attempt writes
    the whole image before the sha check fails, so on the N6's 12 MiB slot three attempts run past
    the watch window. Measured: N6 lan came back `missing=- forbidden=- reached=False` -- every
    marker hit, nothing promoted, simply not settled in time -- while the RT1060 (4 MiB) passed.
    `corrupt_sha` documents the same trap and only dodges it because its device never leaves golden.
    """
    then = ota_cycle.SCENARIOS["reinstall"]["then"]
    assert then.get("by_marker") is True
    assert "install.reject_sha" in then["expect"], "the gate firing is still the point"
    assert "confirm.promoted" in then["forbid"], "and a bad image must still never promote"


def test_run_cycle_lets_a_caller_force_marker_scoring():
    """The board-config route (`server_record: False`) is not enough -- a single PHASE may need it
    on a board that is otherwise recorded server-side."""
    import inspect
    sig = inspect.signature(ota_cycle.run_cycle)
    assert "by_marker" in sig.parameters
    assert sig.parameters["by_marker"].default is False, "opt-in only; boards keep their behaviour"
