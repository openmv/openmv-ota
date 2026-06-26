"""Host tests for the device OTA logger (device/log.py, frozen as _ota_log).

Loaded as a file module (under the openmv_ota name) so coverage measures it; the UART
I/O (``_sink``/``log``) is device-only (``pragma: no cover``), the line formatting is
pure and checked here.
"""

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src/openmv_ota/build/device/log.py"
_spec = importlib.util.spec_from_file_location("openmv_ota._log_under_test", str(_SRC))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_format_kernel_style():
    assert _mod._format(0, "boot", "hi") == "[    0.000] boot: hi\r\n"
    assert _mod._format(12345, "ota", "writing FRONT") == "[   12.345] ota: writing FRONT\r\n"
    assert _mod._format(1, "x", "y") == "[    0.001] x: y\r\n"
    assert _mod._format(3600000, "t", "z") == "[ 3600.000] t: z\r\n"


def test_log_disabled_is_noop():
    # Off by default -> log() returns before importing time / touching a UART, so it's
    # safe even on the host (where time.ticks_ms doesn't exist).
    assert _mod.ENABLED is False
    assert _mod.log("tag", "msg") is None
