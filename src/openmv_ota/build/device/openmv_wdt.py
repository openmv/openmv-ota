"""OTA watchdog helper -- frozen into the firmware as ``openmv_wdt``.

Scaffolded into a project at ``device/openmv_wdt.py``; ``build firmware`` freezes it (as
``openmv_wdt``) so the installer and your app share one watchdog. Like ``openmv_log``
it's **yours to edit** and **off by default**.

To use, edit the config below and rebuild firmware: set ``ENABLED`` and pick your board's
``machine.WDT`` id + a timeout. Then feed it from your main loop::

    import openmv_wdt
    while True:
        openmv_wdt.feed()        # the board resets if your loop ever stops feeding it
        ...

A long blocking op (a multi-second flash erase during an OTA install, a model load, ...)
can't feed from the main loop, so wrap it::

    with openmv_wdt.relax():
        do_long_thing()

``relax()`` runs a ``machine.Timer`` whose callback feeds the watchdog at interrupt time,
so the board survives the op **as long as the CPU itself is healthy** (interrupts still
firing) -- effectively suspending the watchdog without disabling it. Use it only around
genuinely long ops; outside ``relax()`` the watchdog still catches a hung main loop.

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
WDT_ID = 0             # machine.WDT id for your board
TIMEOUT_MS = 5000      # reset if not fed within this long (board WDT max may be lower)
TIMER_ID = -1          # machine.Timer id; on OpenMV ports only the soft timer (-1) exists
FEED_HZ = 10           # relax() feed rate; keep well above 1000 / TIMEOUT_MS

_wdt = None
_feed = None           # pre-bound _wdt.feed, so the hard-IRQ callback allocates nothing
_timer = None


def feed():
    """Feed the watchdog (call from your main loop). No-op when the watchdog is off."""
    if _wdt is not None:
        _wdt.feed()  # pragma: no cover (device)  # hil-residual: watchdog-enabled feed; the bench runs ENABLED=False (opt-in), so _wdt stays None and this is skipped -- covered by a future watchdog-enabled HIL scenario


def _tick(t):  # pragma: no cover (device)  # hil-residual-fn: watchdog-enabled ISR callback; only wired when a watchdog is started (ENABLED=True, opt-in) -- covered by a future watchdog-enabled HIL scenario
    _feed()    # pre-bound method -- no attribute lookup, safe in a hard-IRQ callback


class _Relax:
    """Context manager that feeds the watchdog from a hardware-timer ISR for the duration
    of a long blocking op, then stops -- so the watchdog goes back to needing the main
    loop afterward. A no-op when the watchdog is off."""

    def __enter__(self):
        global _timer
        if _wdt is not None and _timer is None:  # pragma: no cover (device)  # hil-residual: watchdog-off guard (ENABLED=False on the bench -> body skipped)
            import machine  # hil-residual: watchdog-enabled timer setup (opt-in; future watchdog HIL scenario)
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


def _start():  # pragma: no cover (device)  # hil-residual-fn: starts the hardware watchdog; runs only under ENABLED=True (opt-in manual edit + firmware rebuild) -- covered by a future watchdog-enabled HIL scenario
    global _wdt, _feed
    if _wdt is None:
        import machine
        _wdt = machine.WDT(WDT_ID, TIMEOUT_MS)
        _feed = _wdt.feed


if ENABLED:  # pragma: no cover (enabling is a manual edit + firmware rebuild)
    _start()
