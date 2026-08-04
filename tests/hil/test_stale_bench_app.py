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
