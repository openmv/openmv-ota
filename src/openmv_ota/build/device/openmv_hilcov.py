"""HIL coverage markers over a hardware side-channel UART (the P4/P5 pins).

TEST INSTRUMENTATION ONLY, and inert by default. ``mark(point)`` writes a single
``HILCOV <point>`` line to a UART so the bench can record which code paths a live
OTA run actually executed -- across every reboot, without touching the USB-CDC
console (opening that DTR-resets the board) or depending on the network flushing
before ``machine.reset()``.

It is enabled ONLY when ``/flash/.hilcov_uart`` exists and names a UART bus (the
bench writes it -- e.g. ``3`` on the N6 / ``1`` on the AE3, the UART on P4/P5). A
production board never has that file, so ``mark()`` is a no-op and no UART is ever
opened. Frozen alongside the other survival modules so it is in RAM and callable
from boot.py AND the RAM-exec'd installer (which runs after erasing its own slot).

RAM BUDGET: opens at most ONE UART once, then writes short fixed-size marker lines
(the point names are compile-time constants we control). No buffering, no queue,
nothing sized by the wire or a file.
"""

_uart = None
_state = 0                                   # 0 = untried, 1 = active, -1 = disabled
_UART_FILE = "/flash/.hilcov_uart"           # the bench writes the P4/P5 UART bus here


def mark(point):
    """Emit ``HILCOV <point>`` on the coverage UART, or nothing if not enabled."""
    global _state
    if _state == 0:
        _state = 1 if _setup() else -1       # decide once, then it is a cheap check
    if _state > 0:
        _emit(point)


def _setup():
    """True once the coverage UART is open. Off-device (no bench file) this returns
    False and ``mark()`` stays inert; opening the UART itself is device-only."""
    try:
        with open(_UART_FILE) as f:
            bus = int(f.read(8).strip())     # bounded: the file is a single UART bus number
    except Exception:
        return False                         # no bench file / unreadable -> inert
    return _open_uart(bus)


def _open_uart(bus):  # pragma: no cover  (device only)
    global _uart
    from machine import UART
    _uart = UART(bus, baudrate=115200)
    return True


def _emit(point):  # pragma: no cover  (device only)
    try:
        _uart.write(("HILCOV %s\n" % point).encode())
    except Exception:
        pass                                 # a coverage marker must never break the OTA path
