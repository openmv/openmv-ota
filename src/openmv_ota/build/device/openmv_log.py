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
        return _format(_stamp(time.localtime(), time.ticks_ms()),  # hil-residual: the formatter itself -- every marker line on the coverage UART is its output, which is proof it ran; it cannot self-witness (it formats the markers). _stamp/_format are host-tested.
                       record.levelname, record.name, record.message)


def _release_repl(bus):  # pragma: no cover  (device: the REPL binding)  # hil-residual-fn: runs BEFORE the log stream exists, so no marker can witness it -- a marker is a line written to the very UART this is clearing. Its decision is host-tested in tests/build/test_log.py; the bench proves it transitively, since the Portenta's markers arrive at all only once the REPL is off that bus
    """Take the bus: stop the REPL sharing the UART the log is about to write to.

    On most boards the log's UART is a spare one. On some it is the board's REPL UART --
    the Portenta's UART1 is ``MICROPY_HW_UART_REPL`` -- and then the bus has two owners.
    The REPL keeps reading RX, so any byte that lands there is interpreted: a 0x04 among
    them is a soft reboot, whose banner goes straight back out the same TX, to be read and
    interpreted again. A Portenta on the bench produced 8.3 MILLION lines that way in under
    four minutes, at 37k lines a second, burying every real marker under its own noise.

    A log stream and a REPL cannot share a bus, so whoever asks for the log gets it. Only
    the REPL on THIS bus is detached: a REPL on some other UART is someone's console and
    is left alone. Ports without a movable UART REPL have nothing to do here."""
    try:
        import pyb
        current = pyb.repl_uart()
    except (ImportError, AttributeError):  # hil-residual: no pyb.repl_uart (mimxrt/alif/rp2 -- their REPL is not on a UART this can move)
        return  # hil-residual: bare return (nothing to release on this port)
    if current is None:
        return  # hil-residual: no UART REPL attached -- the ordinary case, nothing to release
    # "UART(1, baudrate=115200, ...)" -> "1". Compared as the bus id rather than by
    # identity: the REPL's object is not the one we are about to build.
    text = str(current)
    same = text.split("(", 1)[1].split(",", 1)[0].strip() if "(" in text else ""
    if same == str(bus):
        pyb.repl_uart(None)  # hil-residual: only reachable on a board whose REPL UART IS the log's (the Portenta); no marker can precede the stream that carries markers


def _configure():  # pragma: no cover  (device: handler/UART; runs only when enabled)
    if UART is None:
        import sys  # hil-residual: USB/REPL branch (no UART); the bench always names a UART via /flash/.hilcov_uart so the else branch runs
        stream = sys.stdout  # hil-residual: bare assign (USB/REPL stream; not the bench path)
    else:
        import machine  # hil-residual: witnessed transitively -- "log: configured" reaches the harness's side-channel UART ONLY if the machine.UART stream below was created (the sys.stdout branch would go to USB, not this UART)
        _release_repl(UART)  # hil-residual: witnessed transitively -- on the one board whose REPL shares this bus (the Portenta) the markers below arrive at all only because this ran; it cannot self-witness, since it is clearing the bus the markers travel on
        stream = machine.UART(UART, BAUD)   # hil-residual: the coverage UART stream; its existence is proven by every marker line the harness reads off it
    handler = logging.StreamHandler(stream)
    handler.terminator = "\r\n"
    handler.setFormatter(_OtaFormatter())
    log.addHandler(handler)
    log.setLevel(LEVEL)
    log.debug("log: configured")             # first line once the handler is live -- witnesses _configure


_BENCH_VOLUMES = ("/sdcard", "/flash",   # SD first: when a card is present it is what USB-MSC shows
                  "/rom")                # ...and the read-only romfs LAST, where the bench can bake
#                                          the file into the image. That path needs no CDC, no REPL
#                                          and no writable filesystem, so it still works on a board
#                                          whose app has ARMED A WATCHDOG -- where any REPL touch
#                                          kills the app, the feed stops and the board resets, so
#                                          the file can never be delivered any other way.


def _bench_uart(paths=None):
    """A HIL bench opt-in: this file (bench-written) names a UART to stream the log to --
    the P4/P5 side-channel -- so the harness can watch boot/install/confirm (and the
    HILCOV coverage markers) across every reboot, without the USB REPL (opening which
    DTR-resets the board). Absent on a production board -> None. Host-testable.

    Looked up on EVERY writable volume, not just /flash, because the harness may drop it in over
    USB-MSC and **what MSC exposes varies by board**: with an SD card inserted it is the card
    (mounted /sdcard), without one it is internal flash (/flash). A single hardcoded path would
    silently find nothing on an SD-equipped board -- no coverage UART, so every marker vanishes and
    the run looks like a dead board rather than a misplaced file."""
    if paths is None:
        paths = [v + "/.hilcov_uart" for v in _BENCH_VOLUMES]
    elif isinstance(paths, str):
        paths = [paths]                     # a single path stays valid; iterating a str would
        #                                     walk it CHARACTER by character and open("/") instead
    for path in paths:
        try:
            with open(path) as f:
                return int(f.read(8).strip())   # bounded: the file is a single UART bus number
        except Exception:
            continue
    return None


_bench = _bench_uart()                 # a bench board opts into UART logging via the file
if ENABLED or _bench is not None:  # pragma: no cover  (device: handler / UART, or the bench file)
    if _bench is not None:
        # HIL wants the WHOLE trace (every path, incl. the DEBUG branch lines the coverage
        # checklist keys on) on the side-channel UART -- so bench mode logs at DEBUG.
        UART, LEVEL = _bench, logging.DEBUG
    _configure()
