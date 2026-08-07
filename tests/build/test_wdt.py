"""Host tests for the device OTA watchdog helper (device/openmv_wdt.py, frozen as
openmv_wdt).

Loaded as a file module (under the openmv_ota name) so coverage measures it. Only the
disabled paths are reachable off-device -- the machine.WDT/Timer wiring is device-only
(``pragma: no cover``); on real hardware it's exercised by the app + the installer.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

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


def test_start_is_a_noop_when_disabled():
    # ENABLED is False -> start() must NOT arm the watchdog (nothing to reset an app that
    # never turned it on); _wdt stays None so feed()/relax() stay no-ops too.
    _mod.start()
    assert _mod._wdt is None


def test_relax_is_a_noop_context_when_disabled():
    # no watchdog -> relax() enters/exits without starting a timer
    with _mod.relax():
        pass
    assert _mod._timer is None


# --- the stm32 IWDG guard ---------------------------------------------------
# The IWDG cannot be stopped by software and survives reset, romfs erase AND reflash -- only a
# power cycle clears it, so arming it on a device that might starve it means someone has to go
# physically unplug the board. These pin the guard that keeps that from happening by accident.

class _FakeUname:
    def __init__(self, machine):
        self.machine = machine


def _as_board(monkeypatch, machine_str):
    import os
    monkeypatch.setattr(os, "uname", lambda: _FakeUname(machine_str), raising=False)


def test_stm32_iwdg_is_refused_by_default():
    assert _mod.ALLOW_STM32_IWDG is False        # the safe default is what protects a field device


@pytest.mark.parametrize("wdt_id", [0, "IWDG"])
def test_reject_stm32_iwdg_raises_on_stm32(monkeypatch, wdt_id):
    _as_board(monkeypatch, "OpenMV H7 Plus with STM32H743")
    with pytest.raises(ValueError, match="POWER CYCLE"):   # the message must say how to recover
        _mod._reject_stm32_iwdg(wdt_id, "test")


def test_reject_stm32_iwdg_allows_non_stm32(monkeypatch):
    # mimxrt/alif WDT(0) is the deep-sleep-safe WDOG / alif WDT, NOT an IWDG -- never blocked
    _as_board(monkeypatch, "OpenMV RT1060 with MIMXRT1062")
    assert _mod._reject_stm32_iwdg(0, "test") is None
    _as_board(monkeypatch, "OpenMV AE3 with ALIF")
    assert _mod._reject_stm32_iwdg(0, "test") is None


def test_reject_stm32_iwdg_honours_the_opt_in(monkeypatch):
    # the escape hatch stays available for an app that never deep-sleeps and can guarantee feeding
    _as_board(monkeypatch, "OpenMV H7 Plus with STM32H743")
    monkeypatch.setattr(_mod, "ALLOW_STM32_IWDG", True)
    assert _mod._reject_stm32_iwdg(0, "test") is None


def test_relax_feeds_before_it_allocates():
    """Setting the ISR feed up is itself unfed.

    `import machine` and the Timer construction both allocate, and an allocation can trigger a
    collect -- 221 ms measured on an RT1060 with the heap exhausted, ~44% of that port's 500 ms
    window. Until the timer exists nothing else feeds either: relax() is used from SYNCHRONOUS
    code, so the app's own feed loop is not running. The RT1060 died exactly here -- its
    `erase relax armed` witness never printed, on a step that normally takes 7 ms.
    """
    import inspect
    src = inspect.getsource(_mod._Relax.__enter__)
    body = "\n".join(line.split("#")[0] for line in src.splitlines())
    feed = body.index("_wdt.feed()")
    assert feed < body.index("import machine"), "must feed before the import"
    # ...and again between them: BOTH steps allocate, and two collects back to back (243 ms each,
    # measured on an RT1060 with a full heap) is ~486 ms against a 500 ms window. One feed at the
    # top is not enough -- the RT kept dying right here, just less often.
    between = body.index("_wdt.feed()", body.index("import machine"))
    assert between < body.index("machine.Timer("), "must feed again before the Timer allocation"


def test_relax_nesting_keeps_the_feed_running(monkeypatch):
    """An INNER relax() exiting must not stop the OUTER region's feed.

    __enter__ starts the timer only when there isn't one, but __exit__ used to stop it
    unconditionally -- so a nested use would leave the outer region running unfed to its end,
    silently. Nothing nests today; this pins it before something does.
    """
    events = []

    class _FakeTimer:
        def __init__(self, *a, **k):
            events.append("start")

        def deinit(self):
            events.append("stop")

    class _FakeWdt:
        def feed(self):
            events.append("feed")

    monkeypatch.setattr(_mod, "_wdt", _FakeWdt(), raising=False)
    monkeypatch.setattr(_mod, "_timer", None, raising=False)
    monkeypatch.setattr(_mod, "_depth", 0, raising=False)
    monkeypatch.setitem(sys.modules, "machine", type(sys)("machine"))
    sys.modules["machine"].Timer = _FakeTimer

    with _mod.relax():
        with _mod.relax():
            events.append("inner-body")
        events.append("outer-body-after-inner")

    assert events.count("start") == 1, "the timer must be started once"
    assert events.index("outer-body-after-inner") < events.index("stop"), (
        "the feed must still be running for the rest of the OUTER region")
    assert _mod._depth == 0, "depth must return to zero"


def test_unbalanced_relax_exit_does_not_wedge_the_depth():
    """Defensive: a stray __exit__ must not drive depth negative and disable the feed forever."""
    _mod._depth = 0
    _mod._Relax().__exit__()
    assert _mod._depth == 0


# --- relax() is BOUNDED --------------------------------------------------------------
# A relax() region feeds from a timer ISR, not from progress, so while it is open the
# watchdog cannot catch a stuck call. Unbounded, that turns a blocking C call which never
# returns into a PERMANENT hang on a board whose watchdog is armed -- measured on an N6
# (silent 555 s inside the check-in's relax, watchdog armed, no reset) and an H7 Plus.

def test_relax_stops_feeding_once_its_budget_is_spent(monkeypatch):
    """The core fix: the ISR must give up so the watchdog can bite.

    Simulated by ticking the ISR directly -- the real one is driven by a hardware timer.
    """
    fed = []
    _mod._budget = 0
    monkeypatch.setattr(_mod, "_feed", lambda: fed.append(1), raising=False)
    try:
        _mod._Relax(max_ms=100).__enter__()          # 100 ms at FEED_HZ=50 -> 5 ticks
        assert _mod._budget == 5
        for _ in range(20):                          # tick well past the budget
            _mod._tick(None)
        assert len(fed) == 5, "must feed exactly its budget, then stop"
    finally:
        _mod._budget = 0
        _mod._depth = 0


def test_a_spent_budget_lets_the_watchdog_bite_rather_than_feeding_forever(monkeypatch):
    """Stated as the behaviour that matters: after the budget, further ticks feed NOTHING,
    so the hardware watchdog reaches its timeout and resets the board into a retry."""
    fed = []
    _mod._budget = 0
    monkeypatch.setattr(_mod, "_feed", lambda: fed.append(1), raising=False)
    try:
        _mod._Relax(max_ms=20).__enter__()           # 1 tick
        _mod._tick(None)
        before = len(fed)
        for _ in range(50):
            _mod._tick(None)
        assert len(fed) == before, "a spent region must never feed again"
    finally:
        _mod._budget = 0
        _mod._depth = 0


def test_default_budget_comes_from_relax_max_ms():
    _mod._budget = 0
    try:
        _mod._Relax().__enter__()
        assert _mod._budget == int(_mod.RELAX_MAX_MS * _mod.FEED_HZ / 1000)
    finally:
        _mod._budget = 0
        _mod._depth = 0


def test_nesting_keeps_the_longer_budget():
    """An inner, shorter region must not shorten the outer allowance -- that would reset a
    board that was still legitimately working."""
    _mod._budget = 0
    try:
        _mod._Relax(max_ms=10000).__enter__()
        outer = _mod._budget
        _mod._Relax(max_ms=100).__enter__()
        assert _mod._budget == outer, "the inner region must not shrink the outer budget"
    finally:
        _mod._budget = 0
        _mod._depth = 0


def test_leaving_the_region_clears_the_budget():
    """The ISR must not outlive its region: a leftover budget would feed the watchdog
    through whatever the app does next."""
    _mod._budget = 0
    _mod._depth = 0
    r = _mod._Relax(max_ms=10000)
    r.__enter__()
    assert _mod._budget > 0
    r.__exit__()
    assert _mod._budget == 0


def test_budget_is_the_default_ceiling_for_every_existing_call_site():
    """relax() takes no argument at most call sites, so the ceiling must apply there too --
    otherwise the bug is only fixed where someone remembered to pass a number."""
    import inspect

    src = inspect.getsource(_mod.relax)
    assert "max_ms=None" in src
    assert "RELAX_MAX_MS" in inspect.getsource(_mod._Relax.__init__)


def test_relax_max_ms_stays_within_its_derived_range():
    """Two-sided on purpose, because BOTH directions are real failures.

    Too low resets a healthy-but-slow board -- a device that reboots whenever the network is
    merely slow never finishes anything, which is worse than the hang it was meant to catch.
    The floor is set off the check-in's own socket timeout (openmv_ota._CHECKIN_TIMEOUT = 15 s,
    a few of which can fall inside one region).

    Too high just leaves a wedged device dead for longer. The ceiling keeps anyone from quietly
    inflating this back toward "effectively unbounded", which is the bug this whole change
    exists to fix.
    """
    from openmv_ota.build.device.openmv_ota import _CHECKIN_TIMEOUT

    assert _mod.RELAX_MAX_MS >= 3 * _CHECKIN_TIMEOUT * 1000
    assert _mod.RELAX_MAX_MS <= 90000
