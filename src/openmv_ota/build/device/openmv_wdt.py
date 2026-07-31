"""OTA watchdog helper -- frozen into the firmware as ``openmv_wdt``.

Scaffolded into a project at ``device/openmv_wdt.py``; ``build firmware`` freezes it (as
``openmv_wdt``) so the installer and your app share one watchdog. Like ``openmv_log``
it's **yours to edit** and **off by default**.

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

On every OpenMV port ``machine.Timer`` is the virtual/soft timer (id ``-1`` -- the only
id it accepts), and ``hard=True`` runs its callback in the SysTick/PendSV interrupt
handler. That is what lets the feed fire *while the CPU is blocked* in a flash erase; a
soft (scheduled) callback would wait for the main loop and never run during the op.

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
TIMEOUT_MS = 100       # reset if not fed within this long. MUST be <= the board WDT max (N6 WWDG max
#                        is 167 ms). The deep-sleep-safe watchdog is short by nature -> feed often. If
#                        a port rejects a value this small (a coarse WDOG), raise it to the board min.
TIMEOUT_MS_ALIF = 200  # the alif (AE3) OSPI flash is ~10x slower than the N6/RT: a single 4 KB install
#                        step (or the one unsplittable deflate-decompress C call) can approach 100 ms, so
#                        the AE3 gets a wider window. Still deep-sleep-safe and still tight (alif WDT max
#                        is ~10.7 s). Kept as small as the hardware allows.
TIMER_ID = -1          # machine.Timer id; on OpenMV ports only the soft timer (-1) exists
FEED_HZ = 50           # relax() ISR feed rate (Hz); keep WELL above 1000 / TIMEOUT_MS so it feeds
#                        many times per window (10 Hz was IWDG-era; a ~100 ms window needs ~50+)

_wdt = None
_feed = None           # pre-bound _wdt.feed, so the hard-IRQ callback allocates nothing
_timer = None


def feed():
    """Feed the watchdog (call from your main loop). No-op when the watchdog is off."""
    if _wdt is not None:
        _wdt.feed()  # pragma: no cover (device)  # hil-residual: watchdog-enabled feed; the bench runs ENABLED=False (opt-in), so _wdt stays None and this is skipped -- now exercised on HW by the watchdog HIL scenario (a passing run proves the armed path ran), but marker-less (no log line), so it stays a residual


def _tick(t):  # pragma: no cover (device)  # hil-residual-fn: watchdog-enabled ISR callback; only wired when a watchdog is started (ENABLED=True, opt-in) -- now exercised on HW by the watchdog HIL scenario (a passing run proves the armed path ran), but marker-less (no log line), so it stays a residual
    _feed()    # pre-bound method -- no attribute lookup, safe in a hard-IRQ callback


class _Relax:
    """Context manager that feeds the watchdog from a hardware-timer ISR for the duration
    of a long blocking op, then stops -- so the watchdog goes back to needing the main
    loop afterward. A no-op when the watchdog is off."""

    def __enter__(self):
        global _timer
        if _wdt is not None and _timer is None:  # pragma: no cover (device)  # hil-residual: watchdog-off guard (ENABLED=False on the bench -> body skipped)
            import machine  # hil-residual: watchdog-enabled timer setup (opt-in; exercised on HW by the watchdog HIL scenario)
            _timer = machine.Timer(TIMER_ID, freq=FEED_HZ, hard=True, callback=_tick)  # hil-residual: watchdog-enabled ISR-feed timer (opt-in)
        return self

    def __exit__(self, *args):
        global _timer
        if _timer is not None:  # pragma: no cover (device)  # hil-residual: watchdog-off guard (no timer started on the bench -> body skipped)
            _timer.deinit()  # hil-residual: watchdog-enabled timer teardown (opt-in)
            _timer = None  # hil-residual: watchdog-enabled timer clear (opt-in)
            _wdt.feed()  # hil-residual: watchdog-enabled final feed (opt-in)
        return False


def relax():
    """A context manager that keeps the watchdog fed (via a timer ISR) across a long
    blocking op. No-op when the watchdog is off."""
    return _Relax()


def _start():  # pragma: no cover (device)  # hil-residual-fn: starts the hardware watchdog; runs only under ENABLED=True (opt-in manual edit + firmware rebuild) -- now exercised on HW by the watchdog HIL scenario (a passing run proves the armed path ran), but marker-less (no log line), so it stays a residual
    global _wdt, _feed
    if _wdt is None:
        import machine
        import sys
        timeout = TIMEOUT_MS_ALIF if sys.platform == "alif" else TIMEOUT_MS  # AE3's slow flash -> wider
        if WDT_ID is not None:                        # explicit override
            _wdt = machine.WDT(WDT_ID, timeout)
        else:
            try:                                      # auto-select: stm32/N6 has the deep-sleep-safe
                _wdt = machine.WDT("WWDG", timeout)   # windowed WDT (micropython#19350)...
            except (ValueError, TypeError):           # ...ports without a "WWDG" id (mimxrt/alif) ->
                _wdt = machine.WDT(0, timeout)        # the default deep-sleep-safe WDT
        _feed = _wdt.feed


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
