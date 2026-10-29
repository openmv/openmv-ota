"""The boot stub's lines on the marker UART: which scenarios expect them, and where they come
from. The stub proved every path by OUTCOME on the fleet for a week while its own log never
reached the capture (the HIL project's openmv_log constants said "no UART", and the stub reads
the trailer, not the bench file); these pin the plumbing that closes that gap."""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "ci", "hil"))
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import hil_coverage  # noqa: E402
import ota_cycle  # noqa: E402


@pytest.fixture
def stub_boards(monkeypatch):
    monkeypatch.setattr(ota_cycle, "_has_stub", lambda b: b in ("OPENMV_N6", "OPENMV4P"))


def test_a_stub_board_expects_the_stubs_boot_and_trial_lines(stub_boards):
    expect, forbid = ota_cycle.scenario_markers("OPENMV4P", "delta")
    # the hard-reset boot of the confirmed golden slot: verified, fast path, normal boot ...
    assert {"stub.version", "stub.slot_ok", "stub.fast_path", "stub.boot_normal"} <= expect
    # ... then the install arms a trial and the stub pays for it, installs and boots it
    assert {"stub.slot_trial", "stub.attempt", "stub.install", "stub.record_written",
            "stub.boot_trial"} <= expect
    assert "stub.failed" in forbid and "stub.failed" not in expect


def test_a_refused_update_sees_only_the_golden_boot(stub_boards):
    expect, forbid = ota_cycle.scenario_markers("OPENMV_N6", "bad_sig")
    assert {"stub.version", "stub.slot_ok", "stub.fast_path", "stub.boot_normal"} <= expect
    assert not ({"stub.slot_trial", "stub.attempt", "stub.boot_trial"} & expect)
    assert "stub.failed" in forbid


def test_rollback_sees_the_trial_rejected_and_the_reverted_boot(stub_boards):
    expect, _forbid = ota_cycle.scenario_markers("OPENMV_N6", "rollback")
    assert {"stub.slot_trial", "stub.slot_rejected", "stub.boot_reverted", "stub.install"} <= expect


def test_no_slot_may_give_up_on_both_slots(stub_boards):
    expect, forbid = ota_cycle.scenario_markers("OPENMV_N6", "no_slot")
    assert {"stub.version", "stub.slot_rejected"} <= expect
    assert "stub.failed" not in forbid and "stub.slot_ok" not in expect


def test_a_board_without_a_stub_expects_none_of_it(stub_boards):
    for name in ota_cycle.SCENARIOS:
        expect, forbid = ota_cycle.scenario_markers("OPENMV_RT1060", name)
        assert not any(m.startswith("stub.") for m in expect | forbid), name


def test_every_stub_marker_maps_to_a_line_of_the_stubs_c_source():
    stub_rel = os.path.relpath(hil_coverage.STUB_DIR, hil_coverage.REPO)
    for sub, mid in ota_cycle.COVERAGE.items():
        if not mid.startswith("stub."):
            continue
        rel, lineno = hil_coverage.find_source(sub)
        assert rel and rel.startswith(stub_rel) and lineno, (sub, rel, lineno)


def test_stub_log_lines_read_as_the_uart_sees_them():
    texts = {t for _r, _l, t in hil_coverage.stub_log_lines()}
    assert "stub: firmware area verified" in texts          # log_line adds the prefix
    assert "stub: version" in texts                         # log_kv too (the value is run time)
    assert "stub: reject " in texts                         # log_str prints the literal as is
    assert not any(t.startswith("stub: stub: ") for t in texts)


def test_enable_stub_uart_rewrites_the_projects_log_module(tmp_path, monkeypatch, stub_boards):
    dev = tmp_path / "device"
    dev.mkdir()
    mod = dev / "openmv_log.py"
    mod.write_text("LEVEL = 1\nENABLED = False        # master switch\n"
                   "UART = None            # your board's machine.UART id\nBAUD = 115200\n")
    logged = []
    monkeypatch.setattr(ota_cycle, "log", logged.append)
    assert ota_cycle._enable_stub_uart(str(tmp_path), "OPENMV4P") is True
    text = mod.read_text()
    assert "\nENABLED = True        # master switch\n" in text
    assert "\nUART = 3            # your board's machine.UART id\n" in text    # the 4P's marker UART
    assert "BAUD = 115200" in text and logged and "UART=3" in logged[0]
    # a board without a stub is left alone (its stub settings go nowhere)
    mod.write_text("ENABLED = False\nUART = None\n")
    assert ota_cycle._enable_stub_uart(str(tmp_path), "OPENMV_RT1060") is False
    assert mod.read_text() == "ENABLED = False\nUART = None\n"


def test_enable_stub_uart_refuses_a_module_without_the_defaults(tmp_path, stub_boards):
    dev = tmp_path / "device"
    dev.mkdir()
    (dev / "openmv_log.py").write_text("ENABLED = True\nUART = 7\n")     # someone edited it
    with pytest.raises(RuntimeError, match="could not set ENABLED/UART"):
        ota_cycle._enable_stub_uart(str(tmp_path), "OPENMV4P")
