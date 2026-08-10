"""Host tests for the device OTA watchdog helper (device/openmv_wdt.py, frozen as
openmv_wdt).

Loaded as a file module (under the openmv_ota name) so coverage measures it. Only the
disabled paths are reachable off-device -- the machine.WDT/Timer wiring is device-only
(``pragma: no cover``); on real hardware it's exercised by the app + the installer.
"""

import importlib.util
import inspect
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


def test_relax_does_not_import_inside_the_armed_region():
    """`import machine` used to live in here, and relax() runs INSIDE the armed install (the erase,
    the TLS handshake). An import allocates, and an allocation can trigger an automatic collect --
    65-100 ms on the N6, its entire watchdog window -- so the setup for the thing protecting us was
    itself a chance to be bitten. It is imported at module load now: core MicroPython, always
    present, and boot is unwatched.

    The feed still comes first, because the Timer construction below allocates."""
    import inspect

    src = inspect.getsource(_mod._Relax.__enter__)
    body = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "import " not in body, "no import may run inside a relax region"
    assert body.index("_wdt.feed()") < body.index("_machine.Timer("), "feed before allocating"

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
    fake = type(sys)("machine")
    fake.Timer = _FakeTimer
    monkeypatch.setattr(_mod, "_machine", fake, raising=False)

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


# --- stall_guard(): a watchdog that needs no watchdog ---------------------------------
# The half of the hang that a hardware watchdog cannot cover, because on most boards none is
# armed. A hard-IRQ timer still fires while the main thread is parked in a C call, so progress
# (feed()) can be policed from the ISR and a stall turned into a reset.

def test_feed_reports_progress_to_an_armed_guard(monkeypatch):
    """feed() already marks a step of real work, so the guard needs no new call sites."""
    monkeypatch.setattr(_mod, "_stall_reload", 10, raising=False)
    monkeypatch.setattr(_mod, "_stall_budget", 3, raising=False)
    _mod.feed()
    assert _mod._stall_budget == 10, "a feed must refill the no-progress allowance"


def test_guard_resets_the_board_when_progress_stops(monkeypatch):
    """The whole point: no feed() for the timeout -> machine.reset()."""
    resets = []
    monkeypatch.setattr(_mod, "_reset", lambda: resets.append(1), raising=False)
    monkeypatch.setattr(_mod, "_stall_reload", 3, raising=False)
    monkeypatch.setattr(_mod, "_stall_budget", 3, raising=False)
    for _ in range(3):
        _mod._stall_tick(None)
    assert resets == [], "must not reset while the allowance remains"
    _mod._stall_tick(None)
    assert resets == [1], "a drained allowance must reset the board"


def test_progress_keeps_the_guard_from_firing(monkeypatch):
    """A slow-but-working install must never be killed."""
    resets = []
    monkeypatch.setattr(_mod, "_reset", lambda: resets.append(1), raising=False)
    monkeypatch.setattr(_mod, "_stall_reload", 2, raising=False)
    monkeypatch.setattr(_mod, "_stall_budget", 2, raising=False)
    for _ in range(20):
        _mod._stall_tick(None)
        _mod.feed()                     # the installer feeds as it works
    assert resets == [], "progress must hold the guard off indefinitely"


def test_guard_is_disarmed_on_the_way_out(monkeypatch):
    """Reversible is the property that makes this safe to wrap around just the install --
    unlike WWDG/IWDG, which cannot be turned off once armed. A guard that outlived its region
    would reset the app later, for doing nothing wrong."""
    monkeypatch.setattr(_mod, "_stall_timer", None, raising=False)
    g = _mod.stall_guard(1000)
    g.__exit__()
    assert _mod._stall_reload == 0, "a torn-down guard must not police anything"
    assert _mod._stall_budget == 0


def test_a_disarmed_guard_never_resets(monkeypatch):
    """With reload at 0, feed() leaves the budget at 0 -- so the ISR must not treat an
    unarmed guard as a stall and reset a perfectly healthy board."""
    resets = []
    monkeypatch.setattr(_mod, "_reset", lambda: resets.append(1), raising=False)
    monkeypatch.setattr(_mod, "_stall_reload", 0, raising=False)
    monkeypatch.setattr(_mod, "_stall_budget", 0, raising=False)
    monkeypatch.setattr(_mod, "_stall_timer", None, raising=False)
    _mod.feed()
    assert _mod._stall_budget == 0
    # The ISR is only ever wired while a guard is armed, so an unarmed budget is never ticked.
    assert _mod._stall_timer is None, "no timer means the ISR cannot run"


def test_guard_arms_a_hard_irq_timer_and_tears_it_down(monkeypatch):
    """HARD IRQ is the whole mechanism: a soft-scheduled callback would not run while the main
    thread is parked in a C call, which is exactly the state this exists to escape."""
    made = {}

    class _FakeTimer:
        def __init__(self, tid, freq=None, hard=None, callback=None):
            made["tid"], made["freq"], made["hard"], made["cb"] = tid, freq, hard, callback

        def deinit(self):
            made["deinit"] = True

    fake = type(sys)("machine")
    fake.Timer = _FakeTimer
    fake.reset = lambda: made.setdefault("reset", True)
    monkeypatch.setattr(_mod, "_machine", fake, raising=False)
    monkeypatch.setattr(_mod, "_stall_timer", None, raising=False)
    monkeypatch.setattr(_mod, "_stall_reload", 0, raising=False)
    monkeypatch.setattr(_mod, "_stall_budget", 0, raising=False)

    with _mod.stall_guard(1000):
        assert made["hard"] is True, "must be a hard-IRQ timer"
        assert made["tid"] == _mod.TIMER_ID
        assert _mod._stall_reload == int(1000 * _mod.FEED_HZ / 1000)
        assert _mod._stall_budget == _mod._stall_reload, "must start with a full allowance"
        assert _mod._reset is fake.reset, "machine.reset must be PRE-BOUND (no ISR allocation)"

    assert made.get("deinit") is True, "the timer must not outlive the region"
    assert _mod._stall_timer is None
    assert _mod._stall_reload == 0


def test_nested_guards_keep_policing_the_outer_region(monkeypatch):
    """Nothing nests TODAY -- run()'s check-in guard closes before install() is called, so the
    three guard sites are sequential. This pins the behaviour before something does nest, because
    the failure mode is silent: an inner region exiting would tear the guard down and leave the
    rest of the outer one unpoliced. That is the exact bug relax() shipped with, and it is worth
    one integer not to ship it twice."""
    made = {}

    class _FakeTimer:
        def __init__(self, *a, **k):
            made["n"] = made.get("n", 0) + 1

        def deinit(self):
            made["deinit"] = made.get("deinit", 0) + 1

    fake = type(sys)("machine")
    fake.Timer = _FakeTimer
    fake.reset = lambda: None
    monkeypatch.setattr(_mod, "_machine", fake, raising=False)
    monkeypatch.setattr(_mod, "_stall_timer", None, raising=False)
    monkeypatch.setattr(_mod, "_stall_depth", 0, raising=False)
    monkeypatch.setattr(_mod, "_stall_budget", 0, raising=False)
    monkeypatch.setattr(_mod, "_stall_reload", 0, raising=False)

    with _mod.stall_guard(90000):
        outer = _mod._stall_reload
        with _mod.stall_guard(1000):
            pass
        assert _mod._stall_timer is not None, "the inner exit must NOT tear down the outer guard"
        assert _mod._stall_reload == outer, "an inner region must not shrink the outer allowance"
    assert made.get("deinit") == 1, "torn down exactly once, at the outermost exit"
    assert _mod._stall_depth == 0


def test_unbalanced_guard_exit_does_not_wedge_the_depth(monkeypatch):
    monkeypatch.setattr(_mod, "_stall_depth", 0, raising=False)
    monkeypatch.setattr(_mod, "_stall_timer", None, raising=False)
    _mod._StallGuard(1000).__exit__()
    assert _mod._stall_depth == 0


def test_an_orphaned_timer_must_not_reset_a_healthy_board(monkeypatch):
    """If the stall timer ever outlived its region -- a deinit that did not take -- the ISR would
    see budget 0, read it as "stalled", and reset the board. Forever. Refuse unless armed."""
    resets = []
    monkeypatch.setattr(_mod, "_reset", lambda: resets.append(1), raising=False)
    monkeypatch.setattr(_mod, "_stall_reload", 0, raising=False)   # nothing armed
    monkeypatch.setattr(_mod, "_stall_budget", 0, raising=False)
    for _ in range(50):
        _mod._stall_tick(None)
    assert resets == [], "an unarmed guard must never reset the board"


def test_stall_guard_is_not_wired_into_the_OTA_network_paths():
    """DELIBERATELY UNWIRED. It was measured NOT to rescue a park inside the network stack (an N6
    sat silent 555 s with it armed -- the soft timer's SysTick/PendSV dispatch never ran), and
    wiring it meant a 50 Hz hard IRQ inside the very driver calls that park. Unproven mechanism in
    the hot path of the bug it failed to fix is a bad trade, so run()/install() use plain relax()
    and the hardware watchdog is the backstop.

    The API stays available and tested for apps with a stall the interpreter CAN still be
    scheduled through. This pins the decision so it is not quietly re-wired without new evidence.
    """
    from openmv_ota.build.device import openmv_ota as rt

    src = inspect.getsource(rt)
    assert "stall_guard" not in src, "re-wiring needs evidence it helps; see openmv_wdt docstring"
    assert callable(_mod.stall_guard), "the tool itself stays available to app authors"


# --- the OTA install arms the watchdog itself ------------------------------------------
# User's rule: during an install WE own the device, so we arm it. The app may legitimately
# leave it off (a crash in their own loop is their problem); the install window is ours.

def test_arm_for_install_is_off_pending_the_n6():
    """Off on MEASUREMENT. The armed install dies reproducibly on the N6 at one transition -- the
    last erase-verify chunk to the first written block -- and survived none of: feeding on arm,
    feeding around every allocation there, a proactive collect, preallocating the buffers, or a
    150 ms window. The control (same tree, arming off) installs to 100% every time.

    The N6's WWDG ceiling (~167 ms) sits close to its own worst unavoidable pause (a collect on its
    multi-MB heap is 65-100 ms), which is the suspected squeeze. Ports with a coarser watchdog have
    far more headroom, so revisit per port with measurement rather than fleet-wide."""
    assert _mod.ARM_FOR_INSTALL is False


def test_arm_for_install_never_raises_when_there_is_no_usable_watchdog(monkeypatch):
    """A board whose _start refuses (stm32 with no WWDG -- the IWDG is a one-way door we will
    not arm) must still be able to INSTALL. Unwatched is worse than watched; refusing to update
    is worse than both."""
    def _boom(*a):
        raise ValueError("refusing to arm the stm32 IWDG")

    monkeypatch.setattr(_mod, "ARM_FOR_INSTALL", True)      # the per-port opt-in
    monkeypatch.setattr(_mod, "_start", _boom)
    monkeypatch.setattr(_mod, "_wdt", None, raising=False)
    assert _mod.arm_for_install() is False        # must not raise


def test_arm_for_install_can_be_turned_off(monkeypatch):
    """Kept switchable: a board whose armed install misbehaves must be recoverable without a
    code change (the H7 Plus's armed leg is still the least proven on the fleet)."""
    monkeypatch.setattr(_mod, "ARM_FOR_INSTALL", False)
    assert _mod.arm_for_install() is False


def test_arm_for_install_reports_success_only_when_a_watchdog_is_live(monkeypatch):
    monkeypatch.setattr(_mod, "ARM_FOR_INSTALL", True)      # the per-port opt-in
    monkeypatch.setattr(_mod, "_start", lambda *a: None)   # _start takes the window now
    class _Fake:                                          # arm_for_install feeds right after
        def feed(self):                                   # arming, so the fake needs the method
            pass

    monkeypatch.setattr(_mod, "_wdt", _Fake(), raising=False)
    assert _mod.arm_for_install() is True


def test_the_installer_arms_only_after_every_allocating_step():
    """Placement is the whole safety argument, and it has TWO halves.

    Late enough: the socket, TLS session, deflate window and delta reader are all built before we
    arm, and the write buffer is preallocated before the retry loop. An automatic gc.collect() is
    one unsplittable pause -- 65-100 ms on the N6's multi-MB heap, its entire window -- so an
    allocation inside the armed region is a chance to be bitten for doing nothing wrong. Feeding
    either side of an allocation only narrows that; not allocating closes it.

    Still at the point of no return: on stm32 the WWDG cannot be disarmed by software, so arming
    is only safe where every exit reboots -- which is true from the write onward.
    """
    from pathlib import Path

    import openmv_ota

    src = (Path(openmv_ota.__file__).parent / "build" / "device" / "openmv_ota" / "data"
           / "installer.py").read_text()
    prealloc = src.index("work = bytearray(_CHUNK)")
    loop = src.index("for attempt in range(attempts):")
    open_call = src.index("sock, raw_body = _open(")
    arm = src.index("arm_for_install()", loop)
    write = src.index("_install_stream(source, write,", loop)
    assert prealloc < loop, "the write buffer must be allocated before the retry loop"
    assert open_call < arm, "arm AFTER the socket/TLS allocations"
    assert arm < write, "...and before the write, which is the point of no return"
