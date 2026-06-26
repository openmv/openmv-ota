"""OTA watchdog helper -- frozen into the firmware as ``openmv_wdt``.

Scaffolded into a project at ``device/openmv_wdt.py``; ``build firmware`` freezes it (as
``openmv_wdt``) so the installer and your app share one watchdog. Like ``openmv_log``
it's **yours to edit** and **off by default**.

To use, edit the config below and rebuild firmware: set ``ENABLED``, pick your board's
``machine.WDT`` id + a timeout, and a spare **hardware** ``machine.Timer`` id. Then feed
it from your main loop::

    import openmv_wdt
    while True:
        openmv_wdt.feed()        # the board resets if your loop ever stops feeding it
        ...

A long blocking op (a multi-second flash erase during an OTA install, a model load, ...)
can't feed from the main loop, so wrap it::

    with openmv_wdt.relax():
        do_long_thing()

``relax()`` runs a hardware ``Timer`` whose ISR feeds the watchdog at interrupt time, so
the board survives the op **as long as the CPU itself is healthy** (interrupts still
firing) -- effectively suspending the watchdog without disabling it. Use it only around
genuinely long ops; outside ``relax()`` the watchdog still catches a hung main loop. The
timer MUST be a real hardware id: its callback runs at interrupt time and fires even
while the CPU is blocked; a virtual/soft timer runs via the scheduler and would not.
"""

ENABLED = False        # master switch
WDT_ID = 0             # machine.WDT id for your board
TIMEOUT_MS = 5000      # reset if not fed within this long (board WDT max may be lower)
TIMER_ID = 1           # a spare *hardware* machine.Timer id; used only during relax()
FEED_HZ = 10           # relax() feed rate; keep well above 1000 / TIMEOUT_MS

_wdt = None
_timer = None


def feed():
    """Feed the watchdog (call from your main loop). No-op when the watchdog is off."""
    if _wdt is not None:
        _wdt.feed()  # pragma: no cover (device)


class _Relax:
    """Context manager that feeds the watchdog from a hardware-timer ISR for the duration
    of a long blocking op, then stops -- so the watchdog goes back to needing the main
    loop afterward. A no-op when the watchdog is off."""

    def __enter__(self):
        global _timer
        if _wdt is not None and _timer is None:  # pragma: no cover (device)
            import machine
            _timer = machine.Timer(TIMER_ID, freq=FEED_HZ, callback=lambda t: _wdt.feed())
        return self

    def __exit__(self, *args):
        global _timer
        if _timer is not None:  # pragma: no cover (device)
            _timer.deinit()
            _timer = None
            _wdt.feed()
        return False


def relax():
    """A context manager that keeps the watchdog fed (via a timer ISR) across a long
    blocking op. No-op when the watchdog is off."""
    return _Relax()


def _start():  # pragma: no cover (device)
    global _wdt
    if _wdt is None:
        import machine
        _wdt = machine.WDT(WDT_ID, TIMEOUT_MS)


if ENABLED:  # pragma: no cover (enabling is a manual edit + firmware rebuild)
    _start()
