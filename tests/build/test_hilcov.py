"""Host tests for the device HIL coverage-marker helper (device/openmv_hilcov.py,
frozen as openmv_hilcov).

Loaded as a file module (under the openmv_ota name) so coverage measures it. Only
the disabled/no-op paths are reachable off-device -- the machine.UART wiring is
device-only (``pragma: no cover``); on real hardware it's driven by boot.py + the
installer + the OTA runtime and captured by the bench over the P4/P5 UART.
"""

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src/openmv_ota/build/device/openmv_hilcov.py"


def _load():
    spec = importlib.util.spec_from_file_location("openmv_ota._hilcov_under_test", str(_SRC))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_inert_without_the_bench_file(tmp_path):
    # No /flash/.hilcov_uart -> mark() is a no-op, no UART opened, and it latches
    # disabled so later calls are a cheap early return (not a repeated file open).
    m = _load()
    m._UART_FILE = str(tmp_path / "absent")
    m.mark("install.delta")
    assert m._state == -1 and m._uart is None
    m.mark("install.full")                       # already disabled -> early return branch
    assert m._state == -1


def test_setup_reads_the_bus_and_opens_the_uart(tmp_path):
    # With the bench file present, _setup parses the bus and hands it to the (device-
    # only) UART opener -- mocked here so the parse/dispatch is covered off-device.
    m = _load()
    f = tmp_path / ".hilcov_uart"
    f.write_text("3\n")
    m._UART_FILE = str(f)
    seen = {}
    m._open_uart = lambda bus: seen.update(bus=bus) or True
    assert m._setup() is True
    assert seen["bus"] == 3


def test_emit_writes_the_marker_line_when_enabled():
    # Enabled state -> mark() emits exactly "HILCOV <point>\n" on the UART.
    m = _load()

    class FakeUart:
        def __init__(self):
            self.buf = b""

        def write(self, b):
            self.buf += b

    m._state = 1
    m._uart = FakeUart()
    m.mark("boot.mount.FRONT")
    assert m._uart.buf == b"HILCOV boot.mount.FRONT\n"
