"""OTA debug logging -- frozen into the firmware as ``_ota_log``.

Scaffolded into a project at ``device/log.py``; ``build firmware`` freezes it (as
``_ota_log``) so it's reachable from ``boot.py`` *before* ``/rom`` is mounted, and the
installer and the ``openmv_ota`` runtime lib import it too. Your app can use it as well:
``openmv_ota.log("myapp", "...")`` (or ``import _ota_log``).

It's **yours to edit**. Logging is OFF by default (``log()`` is a no-op, ~zero cost).
To debug on hardware: set ``ENABLED = True``, set ``UART`` to your board's
``machine.UART`` id (the port differs per board), and rebuild firmware. Or repoint
``_sink`` at anything -- ``print()`` to the USB REPL, a file, a socket.

Lines are kernel-style: ``[   SS.mmm] tag: message`` (seconds.ms since boot).
"""

ENABLED = False        # master switch; False -> log() does nothing
UART = None            # machine.UART id for the default _sink; None -> print() to REPL
BAUD = 115200

_uart = None


def _format(ticks_ms, tag, msg):
    """One kernel-style log line from a ms timestamp, a subsystem tag, and a message.
    Pure (no I/O) so it's host-testable; the device bits live in _sink/log."""
    return "[%5d.%03d] %s: %s\r\n" % (ticks_ms // 1000, ticks_ms % 1000, tag, msg)


def _sink(line):  # pragma: no cover  (device I/O -- edit to send logs elsewhere)
    """Write a formatted line out. Default: the configured UART, or the REPL/USB if no
    UART is set. Replace the body to log to a file, a socket, etc."""
    global _uart
    if UART is None:
        print(line, end="")
        return
    if _uart is None:
        import machine
        _uart = machine.UART(UART, BAUD)
    _uart.write(line)


def log(tag, msg):  # pragma: no cover  (calls time + _sink)
    """Emit one structured log line when logging is enabled, else a no-op. Safe to call
    from anywhere -- boot, the installer, the runtime lib, or your app."""
    if not ENABLED:
        return
    import time
    _sink(_format(time.ticks_ms(), tag, msg))
