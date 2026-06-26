"""Host tests for the device OTA watchdog helper (device/openmv_wdt.py, frozen as
openmv_wdt).

Loaded as a file module (under the openmv_ota name) so coverage measures it. Only the
disabled paths are reachable off-device -- the machine.WDT/Timer wiring is device-only
(``pragma: no cover``); on real hardware it's exercised by the app + the installer.
"""

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src/openmv_ota/build/device/openmv_wdt.py"
_spec = importlib.util.spec_from_file_location("openmv_ota._wdt_under_test", str(_SRC))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_disabled_by_default():
    assert _mod.ENABLED is False
    assert _mod._wdt is None and _mod._timer is None and _mod._feed is None


def test_timer_id_is_the_soft_timer():
    # machine.Timer on every OpenMV port is the soft timer; only -1 is accepted, and the
    # feed must run at interrupt time (hard=True) to fire during a blocking erase.
    assert _mod.TIMER_ID == -1


def test_feed_is_a_noop_when_disabled():
    # _wdt is None -> feed() does nothing and never touches machine
    assert _mod.feed() is None


def test_relax_is_a_noop_context_when_disabled():
    # no watchdog -> relax() enters/exits without starting a timer
    with _mod.relax():
        pass
    assert _mod._timer is None
