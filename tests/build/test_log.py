"""Host tests for the device OTA logging config (device/openmv_log.py, frozen as openmv_log).

Loaded as a file module (under the openmv_ota name) so coverage measures it. The pure
timestamp/line formatting is checked here; the logging-record formatter, the UART/handler
setup, and the enable block are device-only (``pragma: no cover``).
"""

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src/openmv_ota/build/device/openmv_log.py"
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


def test_bench_uart_absent_is_none():
    # Production board (no bench file) -> the log stays off, no UART opened.
    assert _mod._bench_uart("/no/such/hilcov_uart") is None


def test_bench_uart_reads_the_bus(tmp_path):
    # The HIL bench opt-in: the file names the P4/P5 UART bus to stream the log to.
    f = tmp_path / ".hilcov_uart"
    f.write_text("3\n")
    assert _mod._bench_uart(str(f)) == 3


def test_bench_uart_searches_every_volume(tmp_path, monkeypatch):
    """What USB-MSC exposes VARIES BY BOARD: with an SD card inserted it is the card (/sdcard),
    without one it is internal flash (/flash). The harness drops .hilcov_uart onto whatever MSC
    shows, so a single hardcoded path would silently find nothing on an SD-equipped board -- no
    coverage UART, every marker gone, and a run that looks like a dead board instead of a misplaced
    file."""
    sd = tmp_path / "sdcard"
    sd.mkdir()
    (sd / ".hilcov_uart").write_text("4")
    assert _mod._bench_uart([str(tmp_path / "flash" / ".hilcov_uart"),
                             str(sd / ".hilcov_uart")]) == 4


def test_bench_uart_default_prefers_the_sd_volume():
    """SD first: when a card is present it is what MSC shows, so it is where the file will be."""
    assert _mod._BENCH_VOLUMES[0] == "/sdcard"
    assert "/flash" in _mod._BENCH_VOLUMES


def test_bench_uart_still_accepts_a_single_path(tmp_path):
    """A str must not be iterated character by character (that would open("/") and friends)."""
    f = tmp_path / ".hilcov_uart"
    f.write_text("2")
    assert _mod._bench_uart(str(f)) == 2
