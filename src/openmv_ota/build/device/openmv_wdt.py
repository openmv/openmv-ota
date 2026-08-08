"""OTA watchdog helper -- frozen into the firmware as ``openmv_wdt``.

Scaffolded into a project at ``device/openmv_wdt.py``; ``build firmware`` freezes it (as
``openmv_wdt``) so the installer and your app share one watchdog. Like ``openmv_log``
it's **yours to edit** and **off by default**.

**WHY AN OTA DEVICE WANTS THIS ARMED -- it is the ONLY thing that recovers a parked board.**
A blocking network call can park in C and never come back: measured repeatedly on this fleet, an
N6 and an H7 Plus each sat silent 555 s inside the check-in, and a Nicla stopped dead mid-download
after its first 4 KB block. Nothing in Python breaks out of that -- the interpreter is not running,
so no timeout fires and no retry happens.

Nor does a software timer. ``stall_guard()`` below arms a hard-IRQ timer meant to reset a stalled
board, and on an N6 `delta` leg it was armed, the check-in parked, and it STILL sat silent for
555 s: a soft timer dispatches through SysTick/PendSV and the scheduler, and a thread parked in
mbedtls/lwIP does not let that run. So the software backstop is not a substitute for this one.

Only the HARDWARE watchdog counts independently of the CPU. Armed, the park becomes a reset and a
retry; unarmed, the device is simply gone until someone power-cycles it.

Use the DEEP-SLEEP-SAFE watchdog (``WDT_ID`` below): the one that STOPS in deep sleep, so it
can't reset you while you sleep. On stm32 that's the WWDG, whose window is **short** -- 167 ms
max on the N6 -- so this is a **tens-of-ms** discipline, not seconds. Edit the config below,
rebuild firmware, and feed it on a TIGHT cadence from your main loop::

    import openmv_wdt
    ...                          # your slow one-time setup (camera reset, network) runs UNWATCHED
    openmv_wdt.start()           # arm now that setup is done -- NOT at import (see start())
    while True:
        openmv_wdt.feed()        # feed every few ms while awake; deep sleep stops the WWDG, so no
        ...                      # feed is needed asleep. A coarse `sleep(2)` loop will reset you.

Feed by REAL PROGRESS -- feed as you do work, so a feed means work happened and a hung loop
still trips the watchdog. Split long ops into short steps and feed per step; the OTA install path
already services it that way (the ranged flash erase feeds per ~2 ms block, etc.). Boot needs no
feeding: ``machine.reset()`` clears the WWDG, so every boot runs before your app re-arms it.

Only as a LAST RESORT, for a single op you truly can't subdivide, wrap it::

    with openmv_wdt.relax():
        do_unsplittable_thing()

``relax()`` runs a ``machine.Timer`` whose ISR feeds the watchdog on a timer -- but that feeds
**regardless of whether your code is making progress**, so for its duration the watchdog is
effectively DISABLED (it can only catch a total CPU/interrupt death, not a stuck loop). Keep its
use rare and its scope minimal; prefer subdividing + progress-based feeding instead.

It is therefore **BOUNDED**: after ``RELAX_MAX_MS`` the ISR stops feeding and the watchdog bites.
Without that bound a blocking C call that never returns is fed forever, so an armed board hangs
until it is power-cycled -- the opposite of what arming a watchdog is for, and something we have
now measured on two boards.

On every OpenMV port ``machine.Timer`` is the virtual/soft timer (id ``-1`` -- the only
id it accepts), and ``hard=True`` runs its callback in the SysTick/PendSV interrupt
handler, so it can fire while the CPU is busy in a long C call -- **but only while
interrupts are enabled**.

**It CANNOT feed through a flash erase or program.** This file used to claim the opposite,
and that single wrong sentence cost a long hunt. On mimxrt, ``ports/mimxrt/flash.c`` wraps
every erase/program in ``__disable_irq()`` around an *unbounded* poll of the chip's
write-in-progress bit, so for that whole span SysTick cannot tick, PendSV cannot run, and
this timer cannot dispatch. Anything that locks the scheduler has the same effect.

So the rule is: **enter every unfeedable region on a FULL window** -- feed on the line
*before* the flash op, never after it, and feed on both sides of a ``gc.collect()``. A
collect is one unsplittable pause too: measured at 221-243 ms on an RT1060 with an 8 MB
heap full of small objects, roughly half that port's 500 ms window. ``relax()`` itself
feeds before it allocates for exactly this reason -- setting the ISR feed up is unfed.

RAM BUDGET: this module runs inside your application, so its memory is your
memory. Every buffer here has a ceiling. Nothing is sized by a file's length, a
response body, a length field off the wire, or a queue that grows while the
network is down: reads use bounded windows of a few KB, anything larger is
streamed, and large data is aliased with memoryview/bytearray_at rather than
copied.
"""

ENABLED = False        # master switch
# WDT_ID selects the watchdog. Leave None to AUTO-SELECT the DEEP-SLEEP-SAFE one for this port (so it
# can't reset you WHILE asleep) -- or set it explicitly to override. Auto-selection:
#   stm32 (N6): "WWDG" -- the windowed watchdog; STOPS in deep sleep. Needs micropython#19350 (the OTA
#               tool carries it). Its max is SHORT (167 ms on the N6), so TIMEOUT_MS must be <= that
#               and you must feed on a tens-of-ms cadence. ("IWDG"/0 is the independent watchdog: it
#               keeps counting THROUGH deep sleep and will reset a sleeping device -- only use it if
#               your app never deep-sleeps.)
#   mimxrt (RT) / alif (AE3): 0 -- the default machine.WDT (WDOG / alif WDT), off in deep sleep. (auto-
#               selection falls back to 0 whenever the port has no "WWDG" id.)
WDT_ID = None
# The stm32 IWDG is a ONE-WAY DOOR: once armed it cannot be stopped by software, and it survives
# machine.reset(), a romfs erase, AND a firmware reflash -- ONLY A POWER CYCLE clears it. So a device
# that starves it in the field reset-loops forever and no OTA update can rescue it: someone has to
# physically unplug it. (That is not hypothetical -- it cost this project a bench board and a manual
# power cycle to diagnose.) It is therefore refused on stm32 by DEFAULT, whether it would come from
# WDT_ID or from auto-selection. Set this True ONLY if you accept that risk: your app must never deep-
# sleep (the IWDG keeps counting through it) AND must feed reliably enough that a starve is impossible.
# Non-stm32 ports are unaffected -- their WDT(0) is the deep-sleep-safe WDOG / alif WDT, not an IWDG.
ALLOW_STM32_IWDG = False
# ARM THE WATCHDOG FOR AN OTA INSTALL, even when ENABLED is False. During an install WE own the
# device -- the app has stopped, /rom is being erased, and a park anywhere in that window leaves a
# board that cannot boot and cannot be recovered remotely. A park is only escapable by a HARDWARE
# reset (a soft timer does not dispatch through it -- measured, see stall_guard), so the watchdog
# is not defence in depth here, it is the whole defence.
#
# Safe because the install is a REGION WITH NO NORMAL EXIT: every path out of the write loop
# reboots (success -> reset into the trial, retry-exhaustion -> reset to golden), and a reset
# clears the WWDG. That matters on stm32, where the watchdog cannot be disarmed by software -- so
# arming it anywhere the code might simply RETURN would leave the app owing a feed forever. It is
# armed at the point of no return, never before: a pre-erase failure still raises to the app with
# nothing armed and nothing erased.
#
# What this does NOT cover is the app's own code. If you leave ENABLED False, a crash or hang in
# your loop is still yours to catch; we only guarantee the window we are driving.
# DEFAULT OFF, on MEASUREMENT. Turning this on reset boards mid-install: the Nicla went from 9/9
# to failing `full` and `corrupt_sha` (the 2 MB writes) while the small scenarios still passed, and
# the whole fleet went 6-green to 3-green. The board reboots between `install: readback` and the
# next block, which is a WWDG bite -- MicroPython's stm32 reset_cause() decodes the IWDG flag and
# not the WWDG one, so it surfaces as reset_cause=0 rather than 3, which is why this did not look
# like a watchdog at first glance.
#
# The cause is structural, not a stray slow step: the stm32 WWDG maxes at ~167 ms (N6) and this
# module runs a 100 ms window, so an armed install has no margin for jitter across a multi-MB
# write. The mimxrt WDOG (500 ms min) and the alif WDT are far more forgiving -- so this is worth
# re-enabling PER PORT once each one's install path is measured armed, not fleet-wide on a guess.
#
# The capability stays because the reasoning behind it is right: during an install the app has
# stopped and only a hardware reset escapes a park. What is missing is the evidence that each
# board's install path fits inside its own window.
ARM_FOR_INSTALL = False
TIMEOUT_MS = 100       # reset if not fed within this long. MUST be <= the board WDT max (N6 WWDG max
#                        is 167 ms). The deep-sleep-safe watchdog is short by nature -> feed often. If
#                        a port rejects a value this small (a coarse WDOG), raise it to the board min.
#                        One window for every port: HIL-validated at 100 ms on the N6 (WWDG), RT1060
#                        (WDOG) and AE3 (alif WDT) -- the AE3's slow OSPI needs no wider window because
#                        the install feeds it per step (the multi-second first-byte download wait, which
#                        once bit the AE3, is handled in the installer's main-thread-fed reader, not here).
TIMER_ID = -1          # machine.Timer id; on OpenMV ports only the soft timer (-1) exists
FEED_HZ = 50           # relax() ISR feed rate (Hz); keep WELL above 1000 / TIMEOUT_MS so it feeds
#                        many times per window (10 Hz was IWDG-era; a ~100 ms window needs ~50+)
# HOW LONG relax() MAY FEED BEFORE IT GIVES UP. This is what stops relax() from turning a hang into
# an ETERNAL hang. Its ISR feeds on a timer, NOT on progress, so for the life of the region the
# watchdog is effectively disabled -- and a blocking C call that never returns is fed forever: the
# device sits there, armed watchdog and all, until someone power-cycles it. That is measured, not
# theoretical (an N6 sat silent for 555 s inside the check-in's relax with the watchdog ARMED, and
# an H7 Plus did the same). Past this budget the ISR stops feeding, the watchdog bites, and the
# board reboots into a retry -- which is the whole point of arming one.
# DERIVED, not picked. Both directions are dangerous, so the number is sized off the real ops:
#   * too LOW resets a healthy-but-slow board, and a board that resets every time the network is
#     merely slow is worse than the hang -- it never finishes anything.
#   * too HIGH just leaves the device dead for longer before it recovers.
# The governing bound is the check-in's own socket timeout (openmv_ota._CHECKIN_TIMEOUT = 15 s),
# which caps the TLS handshake and each recv; a pathological check-in walks a few of those in one
# region, so ~60 s is roughly 4x the worst REAL case. The other long region, the NTP walk, is
# smaller than it looks: openmv_rtc tries the configured host plus ONE rotating fallback per sync
# at a 4 s timeout each, so ~20 s, not the whole fallback list.
# Keep this comfortably above both. It is a HANG bound, not a performance knob.
RELAX_MAX_MS = 60000

_wdt = None
_feed = None           # pre-bound _wdt.feed, so the hard-IRQ callback allocates nothing
_timer = None
_depth = 0             # relax() nesting depth; the ISR feed stops only when it returns to 0
_budget = 0            # ISR feeds REMAINING (ticks). Counts down in _tick; at 0 the feeding stops
#                        and the watchdog is allowed to bite. See RELAX_MAX_MS.
_stall_timer = None    # stall_guard()'s hard-IRQ timer
_stall_budget = 0      # ticks of NO PROGRESS remaining before stall_guard() resets the board
_stall_reload = 0      # what feed() reloads _stall_budget to (0 = no guard armed)
_stall_depth = 0       # stall_guard() nesting depth; the guard is torn down only at 0
_reset = None          # pre-bound machine.reset, so the stall ISR allocates nothing


def feed():
    """Feed the watchdog (call from your main loop). No-op when the watchdog is off.

    Also reports PROGRESS to an armed ``stall_guard()`` -- every feed already marks a step of
    real work, so the guard needs no new call sites."""
    global _stall_budget
    _stall_budget = _stall_reload
    if _wdt is not None:
        _wdt.feed()  # pragma: no cover (device)  # hil-residual: watchdog-enabled feed; the bench runs ENABLED=False (opt-in), so _wdt stays None and this is skipped -- now exercised on HW by the watchdog HIL scenario (a passing run proves the armed path ran), but marker-less (no log line), so it stays a residual


def _tick(t):  # pragma: no cover (device)  # hil-residual-fn: watchdog-enabled ISR callback; only wired when a watchdog is started (ENABLED=True, opt-in) -- now exercised on HW by the watchdog HIL scenario (a passing run proves the armed path ran), but marker-less (no log line), so it stays a residual
    # Feed only while the region still has budget. Small-int arithmetic on a module global:
    # no allocation, so this stays legal in a hard-IRQ callback (which runs under gc_lock).
    global _budget
    if _budget > 0:
        _budget -= 1
        _feed()   # pre-bound method -- no attribute lookup, safe in a hard-IRQ callback
    # else: BUDGET SPENT -- stop feeding on purpose and let the watchdog bite. A region that has
    # run this long is not slow, it is stuck; a reset is the only way back (see RELAX_MAX_MS).


class _Relax:
    """Context manager that feeds the watchdog from a hardware-timer ISR for the duration
    of a long blocking op, then stops -- so the watchdog goes back to needing the main
    loop afterward. A no-op when the watchdog is off."""

    def __init__(self, max_ms=None):
        self._ticks = int((RELAX_MAX_MS if max_ms is None else max_ms) * FEED_HZ / 1000)

    def __enter__(self):
        global _timer, _depth, _budget
        _depth += 1
        # Nesting keeps the LONGER budget: an inner region must not shorten the outer one's
        # allowance and reset a board that was still legitimately working.
        if self._ticks > _budget:
            _budget = self._ticks
        if _wdt is not None and _timer is None:  # pragma: no cover (device)  # hil-residual: watchdog-off guard (ENABLED=False on the bench -> body skipped)
            # FEED FIRST. Setting the ISR feed up is itself unfed: `import machine` and the Timer
            # allocation below both allocate, and an allocation can trigger a collect -- measured
            # at 221 ms on an RT1060 with the heap exhausted, ~44% of that port's 500 ms window.
            # Until the timer exists nothing else is feeding either: relax() is used from
            # SYNCHRONOUS code, so the app's own feed loop is not running. Entering on a partial
            # window is how the RT1060 died here -- its `erase relax armed` witness never printed
            # while the step normally takes 7 ms.
            # ...and feed BETWEEN the two, because there are TWO allocating steps here, not one.
            # The import can trigger a collect and so can the Timer construction; a collect is
            # measured at up to 243 ms on an RT1060 with a full heap, so two of them back to back
            # is ~486 ms against a 500 ms window -- which is why feeding only once at the top was
            # not enough. The RT1060 kept dying right here (`erase loop entered` printed,
            # `erase relax armed` never did, reset_cause=3), just far less often than before.
            _wdt.feed()  # hil-residual: watchdog-enabled pre-setup feed (opt-in; marker-less)
            import machine  # hil-residual: watchdog-enabled timer setup (opt-in; exercised on HW by the watchdog HIL scenario)
            _wdt.feed()  # hil-residual: watchdog-enabled pre-Timer feed (opt-in; marker-less)
            _timer = machine.Timer(TIMER_ID, freq=FEED_HZ, hard=True, callback=_tick)  # hil-residual: watchdog-enabled ISR-feed timer (opt-in)
        return self

    def __exit__(self, *args):
        global _timer, _depth, _budget
        # COUNT THE NESTING. __enter__ starts the timer only when there isn't one, but this used to
        # stop it unconditionally -- so an INNER relax() exiting would kill the OUTER region's feed
        # and leave it running unfed to its end, silently. Nothing nests today; this is the same
        # class of defect as feeding after the op instead of before, and it costs one integer.
        _depth -= 1
        if _depth < 0:                       # unbalanced use: never leave it wedged below zero
            _depth = 0
        if _depth == 0:
            _budget = 0                      # region over -- the ISR must not outlive it
        if _timer is not None and _depth == 0:  # pragma: no cover (device)  # hil-residual: watchdog-off guard (no timer started on the bench -> body skipped)
            _timer.deinit()  # hil-residual: watchdog-enabled timer teardown (opt-in)
            _timer = None  # hil-residual: watchdog-enabled timer clear (opt-in)
            _wdt.feed()  # hil-residual: watchdog-enabled final feed (opt-in)
        return False


def relax(max_ms=None):
    """A context manager that keeps the watchdog fed (via a timer ISR) across a long
    blocking op. No-op when the watchdog is off.

    BOUNDED ON PURPOSE. The ISR feeds on a timer, not on progress, so while the region
    is open the watchdog cannot catch a stuck loop -- and a blocking C call that never
    returns would otherwise be fed FOREVER, leaving an armed board hung until someone
    power-cycles it. After ``max_ms`` (default ``RELAX_MAX_MS``) the ISR stops feeding
    and the watchdog is allowed to do its job. Pass a smaller ``max_ms`` for a region
    you know is short; keep it comfortably above the op's real worst case, because
    overrunning the budget resets the board."""
    return _Relax(max_ms)


def _stall_tick(t):  # pragma: no cover (device)  # hil-residual-fn: stall_guard ISR; fires only while a guard is armed (install path), and its only observable effect is the reset it triggers
    # NO PROGRESS accounting. feed() reloads the budget from the main thread; this drains it.
    # Small-int arithmetic on a module global -- no allocation, legal in a hard-IRQ callback.
    global _stall_budget
    if not _stall_reload:
        return     # NO GUARD ARMED. Belt-and-braces: if this timer ever outlived its region (a
        #            deinit that did not take), an unarmed budget of 0 would otherwise read as
        #            "stalled" and reset a perfectly healthy board, forever. Refuse to act unless
        #            a region actually armed us.
    if _stall_budget > 0:
        _stall_budget -= 1
    else:
        _reset()   # pre-bound machine.reset -- the ONLY exit from a C call that never returns


class _StallGuard:
    """See stall_guard()."""

    def __init__(self, timeout_ms):
        self._ticks = int(timeout_ms * FEED_HZ / 1000)

    def __enter__(self):
        global _stall_timer, _stall_budget, _stall_reload, _reset, _stall_depth
        # COUNT THE NESTING, for the same reason relax() has to: an inner region exiting must not
        # tear down the outer one's guard and leave the rest of it unpoliced. run() guards the
        # check-in and install() guards itself, so these DO nest the moment an install is offered.
        _stall_depth += 1
        if self._ticks > _stall_budget:
            _stall_budget = self._ticks       # nesting keeps the LONGER allowance
            _stall_reload = self._ticks
        if _stall_timer is None:  # pragma: no cover (device)  # hil-residual: device-only arm (host has no machine.Timer)
            import machine  # hil-residual: stall-guard timer setup
            _reset = machine.reset  # hil-residual: pre-bound so the ISR allocates nothing
            _stall_reload = self._ticks  # hil-residual: what feed() reloads to
            _stall_budget = self._ticks  # hil-residual: start with a full allowance
            _stall_timer = machine.Timer(TIMER_ID, freq=FEED_HZ, hard=True, callback=_stall_tick)  # hil-residual: hard IRQ so it fires while the main thread is parked in C
        return self

    def __exit__(self, *args):
        global _stall_timer, _stall_reload, _stall_budget, _stall_depth
        _stall_depth -= 1
        if _stall_depth < 0:                  # unbalanced use: never wedge below zero
            _stall_depth = 0
        if _stall_depth:                      # an OUTER region is still running -- keep policing it
            return False
        if _stall_timer is not None:  # pragma: no cover (device)  # hil-residual: device-only teardown
            _stall_timer.deinit()  # hil-residual: the guard must not outlive its region
            _stall_timer = None  # hil-residual: bare clear
        _stall_reload = 0
        _stall_budget = 0
        return False


def stall_guard(timeout_ms):
    """Reset the board if NO PROGRESS is made for ``timeout_ms`` -- a watchdog that needs no
    watchdog, and that can be TURNED OFF again.

    This exists because a blocking network call can park in C and never return, and nothing in
    Python can break out of that: the interpreter is not running, so no timeout fires and no
    retry happens. On a board with no hardware watchdog armed that state is permanent.

    The idea is that a HARD-IRQ timer keeps firing while the main thread sits in a C call.
    ``feed()`` reloads the budget from the main thread, so every existing feed doubles as a
    progress report; when the budget drains, the ISR calls ``machine.reset()``.

    **MEASURED LIMIT -- this does NOT rescue a park inside the network stack.** On an N6 `delta`
    leg the check-in parked with this guard armed and the board sat silent for 555 s: the ISR
    never dispatched, so the reset never came. A soft timer is delivered through SysTick/PendSV
    and the MicroPython scheduler, and a thread parked inside mbedtls/lwIP evidently does not let
    that dispatch run. A `time.sleep()` bench test DOES trigger it -- sleep yields to the
    scheduler -- so that test proved the mechanism, not the case that matters. Do not read a
    passing bench check as coverage of the real hang.

    What this leaves it good for: stalls where the interpreter is still being scheduled (a Python
    loop that stops making progress, a driver call that does allow ISRs). For a park inside a
    blocking C call the ONLY thing that gets the board back is the HARDWARE watchdog, which
    counts independently of the CPU -- which is why ENABLED matters and why relax() had to stop
    feeding blindly.

    Unlike the stm32 hardware watchdogs this is REVERSIBLE -- a soft timer can be deinit'd, where
    WWDG/IWDG cannot be turned off once armed. That is what makes it safe to wrap around just the
    install and leave the app with no lasting obligation to feed anything. It touches no hardware
    watchdog, so it cannot start the IWDG.

    Size ``timeout_ms`` above the longest legitimate no-progress gap (see RELAX_MAX_MS): a false
    trigger costs one install attempt, which the device then retries -- cheap next to a hang that
    needs a human with a power cable."""
    return _StallGuard(timeout_ms)


def _reject_stm32_iwdg(wdt_id, why):  # pragma: no cover (device)  # hil-residual-fn: the IWDG guard; reaching it needs an stm32 board with WDT_ID=IWDG or a WWDG-less build, neither of which the bench runs (the boards auto-select a working WWDG)
    """Raise rather than arm the stm32 IWDG -- the one watchdog no software can undo (see
    ALLOW_STM32_IWDG). A no-op on every non-stm32 port, where WDT(0) is the deep-sleep-safe WDOG."""
    import os
    if ALLOW_STM32_IWDG or "STM32" not in os.uname().machine:
        return  # hil-residual: bare return (opted in, or not stm32 -> nothing to guard)
    raise ValueError("openmv_wdt: refusing to arm the stm32 IWDG (%s). It cannot be stopped by "
                     "software and survives reset, romfs erase and reflash -- only a POWER CYCLE "
                     "clears it, so a starve bricks the board in the field. Fix the WWDG build/"
                     "config, or set ALLOW_STM32_IWDG=True if you accept that." % why)


def _start():  # pragma: no cover (device)  # hil-residual-fn: starts the hardware watchdog; runs only under ENABLED=True (opt-in manual edit + firmware rebuild) -- now exercised on HW by the watchdog HIL scenario (a passing run proves the armed path ran), but marker-less (no log line), so it stays a residual
    global _wdt, _feed
    if _wdt is None:
        import machine
        if WDT_ID is not None:                        # explicit override
            if WDT_ID in (0, "IWDG"):                 # ...which must not smuggle in the IWDG on stm32
                _reject_stm32_iwdg(WDT_ID, "WDT_ID=%r" % (WDT_ID,))
            _wdt = machine.WDT(WDT_ID, TIMEOUT_MS)
        else:
            try:                                      # auto-select: stm32/N6 has the deep-sleep-safe
                _wdt = machine.WDT("WWDG", TIMEOUT_MS)  # windowed WDT (micropython#19350)...
            except (ValueError, TypeError):           # ...ports without a "WWDG" id fall back to WDT(0).
                # On mimxrt/alif WDT(0) IS the deep-sleep-safe WDOG / alif WDT -- a fine fallback. On
                # stm32 it is the IWDG, so refuse there instead of silently arming it.
                _reject_stm32_iwdg(0, "WWDG unavailable on stm32")
                _wdt = machine.WDT(0, TIMEOUT_MS)       # mimxrt/alif: the default deep-sleep-safe WDT
        _feed = _wdt.feed


def arm_for_install():  # pragma: no cover (device)  # hil-residual-fn: arms real hardware; the host has no machine.WDT, and on the bench a passing install with `install: wdt armed` in the log is the witness
    """Arm the watchdog for an OTA install regardless of ENABLED -- see ARM_FOR_INSTALL.

    Returns True iff a watchdog is now running. NEVER RAISES: a board with no usable watchdog
    (an stm32 whose build has no WWDG, where _start refuses the IWDG rather than arm a
    one-way door) must still be able to install -- unwatched is worse than not at all, but
    refusing to update is worse than both."""
    if not ARM_FOR_INSTALL:
        return False  # hil-residual: opt-out branch; the bench runs with it on
    try:
        _start()
    except Exception as e:
        # The IWDG refusal lands here. Say so once -- silently installing unwatched on a board
        # the operator believes is protected is the kind of gap that only shows up in the field.
        log_unavailable(e)
        return False
    return _wdt is not None


def log_unavailable(e):  # pragma: no cover (device)  # hil-residual-fn: only reachable when _start refuses (stm32 without WWDG); the bench boards all have one
    try:
        from openmv_log import log as _l
        _l.warning("install: no usable watchdog (%r) -- installing UNWATCHED" % (e,))
    except Exception:
        pass  # hil-residual: logging must never be what breaks an install


def start():
    """Arm the watchdog NOW. Call this ONCE from your app, when it is PAST its slow one-time setup
    (camera reset, network bring-up) and about to enter its steady main loop. Arming earlier -- e.g.
    at import -- would let the short window (~100 ms) expire DURING that setup, before your first
    ``feed()``, and reset the board. Idempotent and a no-op when the watchdog is off (ENABLED=False),
    so it is safe to leave in your app unconditionally. Nothing else arms it: with ENABLED=True but no
    ``start()`` call, the watchdog never runs. After an OTA trial reboot ``machine.reset()`` clears the
    WWDG, so boot runs unwatched and your app re-arms here -- boot itself needs no feeding."""
    if ENABLED:
        _start()  # pragma: no cover (device)  # hil-residual: watchdog-enabled arm; ENABLED=False on the bench so start() no-ops -- the ENABLED=True arm is an opt-in edit + rebuild, now exercised on HW by the watchdog HIL scenario
