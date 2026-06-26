"""OTA logging config -- frozen into the firmware as ``openmv_log``.

Built on the standard ``logging`` module (frozen on every OpenMV board via the board
manifest's ``require("logging")``), so the OTA code and your app share one logger tree:

    import logging
    logging.getLogger("openmv_ota").info("hi")     # or openmv_ota.log.info("hi")

``boot.py``, the installer, and the ``openmv_ota`` runtime lib all log to the
``openmv_ota`` logger; importing this module (which the build freezes as ``openmv_log``)
configures it. Records carry a level; the output is timestamped:

    [2026-06-25 12:34:56] WARNING openmv_ota: install: FAILED after erase   (RTC set)
    [   12.345] INFO openmv_ota: boot: mounted FRONT                        (RTC unset)

It prefers **wall-clock UTC from the RTC** -- which is set by the time the installer
runs, since TLS cert validation requires it (``ntptime.settime()``; see the OpenMV TLS
prerequisites). Before the clock is set (e.g. in ``boot.py``, pre-NTP) it falls back to
**monotonic uptime** ``[ seconds.ms ]``. The stock ``logging`` formatter can do neither
(its ``asctime`` needs ``time.strftime``, absent on these ports), hence the small custom
formatter below.

It's **off by default** (the logger's level is set above CRITICAL, so nothing emits and
nothing leaks to the REPL). To debug on hardware, edit the config block below -- set
``ENABLED = True`` and ``UART`` to your board's ``machine.UART`` id -- and rebuild
firmware. Or change ``_configure`` to log to a file/socket/the REPL.
"""

import logging
import time

# --- edit to enable -----------------------------------------------------------
ENABLED = False        # master switch
UART = None            # your board's machine.UART id; None -> the USB REPL (sys.stdout)
BAUD = 115200
LEVEL = logging.INFO   # emit this level and above when enabled
# -----------------------------------------------------------------------------

log = logging.getLogger("openmv_ota")
log.setLevel(logging.CRITICAL + 1)     # OFF: nothing passes isEnabledFor by default


def _stamp(localtime, ticks_ms):
    """The timestamp field: wall-clock UTC when the RTC is set (year >= 2023), else
    monotonic uptime seconds.ms. Pure (takes the time values) so it's host-testable."""
    if localtime[0] >= 2023:
        return "%04d-%02d-%02d %02d:%02d:%02d" % (
            localtime[0], localtime[1], localtime[2], localtime[3], localtime[4], localtime[5])
    return "%5d.%03d" % (ticks_ms // 1000, ticks_ms % 1000)


def _format(stamp, levelname, name, msg):
    """One log line from a preformatted timestamp + the record fields. Pure."""
    return "[%s] %s %s: %s" % (stamp, levelname, name, msg)


class _OtaFormatter(logging.Formatter):  # pragma: no cover  (device record API + clock)
    def format(self, record):
        return _format(_stamp(time.localtime(), time.ticks_ms()),
                       record.levelname, record.name, record.message)


def _configure():  # pragma: no cover  (device: handler/UART; runs only when enabled)
    if UART is None:
        import sys
        stream = sys.stdout
    else:
        import machine
        stream = machine.UART(UART, BAUD)   # created once, kept by the handler
    handler = logging.StreamHandler(stream)
    handler.terminator = "\r\n"
    handler.setFormatter(_OtaFormatter())
    log.addHandler(handler)
    log.setLevel(LEVEL)


if ENABLED:  # pragma: no cover  (enabling is a manual edit + firmware rebuild)
    _configure()
