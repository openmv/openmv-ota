"""`run: cycle failed` summarisation -- the line that turns a marker list into a diagnosis.

The device swallows poll-cycle exceptions and retries, so a board that can NEVER install looks
exactly like a board with nothing on offer: no install markers, a timeout, and thousands of UART
lines. These tests pin the summary that names the fault instead.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ci" / "hil"))

import ota_cycle  # noqa: E402


def _cap(lines):
    return types.SimpleNamespace(raw=list(lines))


REAL = ("[2026-08-04 02:34:39] WARNING openmv_ota: run: cycle failed "
        "MemoryError('memory allocation failed, allocating 262145 bytes',)")


def test_it_counts_the_repeats_of_one_fault():
    """The real shape: one fault, every poll, forever."""
    faults = ota_cycle.device_faults(_cap([REAL] * 47))
    assert list(faults.values()) == [47]
    assert "allocating 262145 bytes" in list(faults)[0]


def test_the_timestamp_and_logger_prefix_are_stripped():
    """Keyed on the exception, not the line -- otherwise every timestamp is a distinct 'fault'."""
    lines = [REAL, REAL.replace("02:34:39", "02:35:44"), REAL.replace("02:34:39", "02:36:50")]
    assert list(ota_cycle.device_faults(_cap(lines)).values()) == [3]


def test_distinct_faults_stay_distinct():
    other = REAL.replace("MemoryError('memory allocation failed, allocating 262145 bytes',)",
                         "OSError(-202,)")
    faults = ota_cycle.device_faults(_cap([REAL, other, REAL]))
    assert len(faults) == 2 and faults[list(faults)[0]] == 2


def test_a_healthy_run_reports_nothing():
    assert ota_cycle.device_faults(_cap(["boot: ready", "install: installed"])) == {}


def test_no_capture_is_not_an_error():
    """The FAIL branch calls this unconditionally; a board with no marker UART must not crash it."""
    assert ota_cycle.device_faults(None) == {}
