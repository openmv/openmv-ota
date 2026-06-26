"""Host tests for the device OTA logging config (device/log.py, frozen as _ota_log).

Loaded as a file module (under the openmv_ota name) so coverage measures it. The pure
timestamp/line formatting is checked here; the logging-record formatter, the UART/handler
setup, and the enable block are device-only (``pragma: no cover``).
"""

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src/openmv_ota/build/device/log.py"
_spec = importlib.util.spec_from_file_location("openmv_ota._log_under_test", str(_SRC))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_stamp_wallclock_when_rtc_set():
    # The RTC is set (year >= 2023) by the time the installer runs (TLS needs it).
    assert _mod._stamp((2026, 6, 25, 12, 34, 56, 0, 0), 999) == "2026-06-25 12:34:56"
    assert _mod._stamp((2023, 1, 2, 3, 4, 5, 0, 0), 0) == "2023-01-02 03:04:05"


def test_stamp_uptime_when_rtc_unset():
    # Before NTP (e.g. in boot.py) the RTC reads the MicroPython epoch -> uptime instead.
    assert _mod._stamp((2000, 1, 1, 0, 0, 0, 0, 0), 12345) == "   12.345"
    assert _mod._stamp((2022, 12, 31, 0, 0, 0, 0, 0), 1) == "    0.001"


def test_format():
    assert _mod._format("12.345", "INFO", "openmv_ota", "hi") == "[12.345] INFO openmv_ota: hi"
    assert (_mod._format("2026-06-25 12:34:56", "WARNING", "openmv_ota", "x")
            == "[2026-06-25 12:34:56] WARNING openmv_ota: x")


def test_logger_is_off_by_default():
    # No handler + level above CRITICAL == silent until the user enables it.
    import logging
    assert _mod.log.level > logging.CRITICAL
