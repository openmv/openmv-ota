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

RAM BUDGET: this module runs inside your application, so its memory is your
memory. Every buffer here has a ceiling. Nothing is sized by a file's length, a
response body, a length field off the wire, or a queue that grows while the
network is down: reads use bounded windows of a few KB, anything larger is
streamed, and large data is aliased with memoryview/bytearray_at rather than
copied.
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


def _bench_uart(path="/flash/.hilcov_uart"):
    """A HIL bench opt-in: this file (bench-written) names a UART to stream the log to --
    the P4/P5 side-channel -- so the harness can watch boot/install/confirm (and the
    HILCOV coverage markers) across every reboot, without the USB REPL (opening which
    DTR-resets the board). Absent on a production board -> None. Host-testable."""
    try:
        with open(path) as f:
            return int(f.read(8).strip())   # bounded: the file is a single UART bus number
    except Exception:
        return None


_bench = _bench_uart()                 # a bench board opts into UART logging via the file
if ENABLED or _bench is not None:  # pragma: no cover  (device: handler / UART, or the bench file)
    if _bench is not None:
        # HIL wants the WHOLE trace (every path, incl. the DEBUG branch lines the coverage
        # checklist keys on) on the side-channel UART -- so bench mode logs at DEBUG.
        UART, LEVEL = _bench, logging.DEBUG
    _configure()
