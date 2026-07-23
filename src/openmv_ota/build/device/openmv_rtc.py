"""``openmv_rtc`` -- a wall clock you can decide whether to trust.

The device records data long before it necessarily knows what time it is. This
module answers one question: *is the current time real?*, and gives you a Unix
timestamp when the answer is yes.

    import openmv_rtc

    openmv_rtc.resolve()            # once, after the network is up
    if openmv_rtc.trusted():
        ts = openmv_rtc.now()       # Unix seconds (UTC), real

HOW TRUST IS DECIDED: the firmware cannot have been running before it was built,
so a clock reading earlier than the build timestamp is provably wrong. That is
the whole test -- no network needed to apply it. A board with a coin cell (or one
waking from deep sleep, where the RTC keeps counting) is therefore trusted
immediately, with no NTP round trip; a board that cold-booted with a dead RTC
reads January 1st of the epoch year, fails the test, and stays untrusted until
:func:`sync` succeeds.

The check is LATCHED, because the RTC counts monotonically: a dead clock left
running long enough would eventually count up past the build and a bare window
test would start believing it (though the reading is really epoch-plus-uptime).
So one out-of-window reading marks the clock bad until it is actually re-set --
a clock that has ever looked wrong is never trusted just because it later looks
right.

WHY AN UNTRUSTED CLOCK REPORTS NOTHING: a wrong timestamp is worse than no
timestamp, because nothing downstream can tell it is wrong. When the clock is
untrusted, records carry only ``(sid, seq)`` and the server falls back to its own
arrival time. ``seq`` is always the exact ordering; a timestamp is a convenience
laid over it.

PORTABILITY -- both of these differ across the ports OpenMV ships, so neither is
assumed:

* ``machine.RTC`` exposes only ``datetime()`` on every port (stm32 also has
  init/calibration/wakeup, alif and mimxrt have alarm, mimxrt has irq, and NO
  port here has ``memory()``), so ``datetime()`` is the only API used.
* The epoch is **not** the same everywhere: alif and mimxrt count from 1970,
  stm32 and rp2 from 2000. ``time.time()`` on an AE3 and an N6 therefore differ
  by 30 years. The offset is detected at import from ``time.gmtime(0)`` rather
  than hardcoded, and everything this module returns is Unix (1970) seconds.

RAM BUDGET: this module runs inside your application, so its memory is your
memory. It holds a few integers and allocates only during a sync.
"""

import time

try:                                  # the build stamps this into _ota_config
    from _ota_config import BUILD_TIME
except ImportError:                   # host, or a non-OTA firmware: no floor
    BUILD_TIME = 0

# Seconds between the two epochs MicroPython ports use (1970-01-01 -> 2000-01-01).
_EPOCH_2000 = 946684800

# How far ahead of the build a clock may read before we call it broken. A real
# device can legitimately run for years, so this is deliberately generous -- it
# only catches a wildly-wrong future reading (a corrupt RTC latching all ones).
_MAX_AHEAD = 20 * 365 * 24 * 3600

_source = "none"                      # "rtc" | "ntp" | "none"
_bad = False                          # latched: an out-of-window reading was seen


def _epoch_offset():
    """Seconds to add to this port's ``time.time()`` to get Unix time. Detected,
    never assumed: ``time.gmtime(0)`` reports the port's own epoch year."""
    return _EPOCH_2000 if time.gmtime(0)[0] == 2000 else 0


def now():
    """The current time as Unix (1970) seconds, whatever the port's epoch. Always
    returns a number -- call :func:`trusted` to find out if it means anything."""
    return time.time() + _epoch_offset()


def _in_window(unix):
    """True if ``unix`` reads at or after the build and not absurdly far past it.
    Pure -- the window arithmetic, without the session latch :func:`trusted`
    applies over it."""
    return BUILD_TIME <= unix <= BUILD_TIME + _MAX_AHEAD


def trusted():
    """True only when the clock is one we can believe: it reads inside the window
    now AND has never read outside it this session (unless re-set since).

    THE LATCH IS THE POINT. The RTC counts monotonically, so a board that cold-
    boots with a dead RTC starts near the epoch -- far below the build -- but
    left running long enough it would eventually *count up past* the build, and a
    bare window check would then believe a time that is really epoch-plus-uptime.
    So a single out-of-window reading marks the clock bad for the rest of the
    session; only setting it from a real source (:func:`set_time`, which NTP sync
    calls) clears that. A clock valid from its very first reading -- one that
    survived deep sleep, or a coin cell -- is trusted with no sync.

    With no build stamp (a non-OTA firmware) there is no floor, so the clock is
    reported untrusted rather than assumed good."""
    global _bad
    if not BUILD_TIME:
        return False
    if not _in_window(now()):
        _bad = True                   # one bad reading and we stop believing it
        return False
    return not _bad


def source():
    """Where the current time came from: ``"rtc"`` (already valid at boot, e.g.
    kept across deep sleep), ``"ntp"`` (synced this boot), or ``"none"``."""
    return _source


def timestamp():
    """The Unix timestamp to attach to a record, or None when the clock is not
    trustworthy -- callers put a ``ts`` field on a record only when this returns
    a number, so a known-bad time is never recorded as if it were real."""
    return now() if trusted() else None


def set_time(unix_s):
    """Set the RTC from Unix seconds. Uses ``RTC().datetime()``, the only setter
    available on every port; the tuple is ``(year, month, day, weekday, hour,
    minute, second, subseconds)`` with weekday 1-7.

    Setting the clock is a known-good time, so it clears the bad-reading latch --
    this is how an NTP sync rescues a clock that :func:`trusted` had given up on."""
    global _bad
    import machine
    tm = time.gmtime(int(unix_s) - _epoch_offset())
    machine.RTC().datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
    _bad = False


def sync(host=None):  # pragma: no cover  (device: network + RTC)
    """Set the clock from NTP. Returns True on success, False if the query failed
    (no network yet, DNS down, UDP blocked) -- a failed sync is a normal state to
    retry, never an exception for the caller to handle.

    ``ntptime`` is frozen into every OpenMV board and already handles the epoch
    difference and the 2036 NTP wrap, so it does the query; this module only
    decides whether the result is worth keeping."""
    global _source
    try:
        import ntptime
        if host:
            ntptime.host = host
        unix = ntptime.time() + _epoch_offset()
        if unix < BUILD_TIME:         # an NTP reply older than the build is bogus
            return False
        set_time(unix)
        _source = "ntp"
        return True
    except Exception:
        return False


def resolve(host=None):  # pragma: no cover  (device: network + RTC)
    """Establish the clock once, cheaply: keep what the RTC already has if it is
    trustworthy (the deep-sleep and coin-cell case -- no network needed), else
    try one NTP sync. Returns True if the clock ended up trustworthy.

    Safe to call repeatedly: once the clock is good it costs a comparison."""
    global _source
    if trusted():
        if _source == "none":
            _source = "rtc"
        return True
    return sync(host)
