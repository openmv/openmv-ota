"""The harness's CDC liveness probe and the YKUSH VBUS revive (ci/hil/ota_cycle.py).

Both exist because of one HIL run (40427373415): the probe's own Ctrl-C makes the bench app soft-
reset, and on a board that boots through the firmware stub the REPL is back a second after
mpremote's 10 s read limit -- so every probe failed, every failure pulsed the J-Link, and one pulse
left the N6 off USB and SWD. The fixes: ask more than once before any pulse, and cut VBUS through
the node's YKUSH XS when the board is gone from USB altogether.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CIHIL = os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil"))
sys.path.insert(0, _CIHIL)
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import ota_cycle  # noqa: E402  (ci/hil, added to sys.path above)


def _probes(monkeypatch, answers):
    """Script mpremote's exit codes; record sleeps."""
    calls, sleeps = [], []
    answers = iter(answers)
    monkeypatch.setattr(ota_cycle, "ota", lambda name: name)
    monkeypatch.setattr(ota_cycle, "sh", lambda cmd, **k: calls.append(cmd) or (next(answers), ""))
    monkeypatch.setattr(ota_cycle.time, "sleep", sleeps.append)
    return calls, sleeps


def test_cdc_answers_asks_again_before_giving_up(monkeypatch):
    calls, sleeps = _probes(monkeypatch, [1, 0])
    assert ota_cycle._cdc_answers() is True
    assert len(calls) == 2 and all("eval" in c for c in calls)
    assert sleeps == [ota_cycle._CDC_PROBE_GAP_S]        # one gap, between the two probes


def test_cdc_answers_stops_at_the_probe_budget(monkeypatch):
    calls, sleeps = _probes(monkeypatch, [1] * 10)
    assert ota_cycle._cdc_answers() is False
    assert len(calls) == ota_cycle._CDC_PROBES
    assert len(sleeps) == ota_cycle._CDC_PROBES - 1       # no gap after the last one


def test_cdc_answers_returns_at_the_first_yes(monkeypatch):
    calls, _ = _probes(monkeypatch, [0, 1, 1])
    assert ota_cycle._cdc_answers() is True
    assert len(calls) == 1


def test_board_absent_means_no_port_and_no_dfu(monkeypatch):
    monkeypatch.setitem(ota_cycle.CFG, "acm", "/dev/ttyACM-none")
    monkeypatch.setattr(ota_cycle, "_dfu_present", lambda: False)
    assert ota_cycle._board_absent() is True
    monkeypatch.setattr(ota_cycle, "_dfu_present", lambda: True)
    assert ota_cycle._board_absent() is False             # in DFU is not absent
    monkeypatch.setattr(ota_cycle, "_dfu_present", lambda: False)
    monkeypatch.setattr(ota_cycle.os.path, "exists", lambda p: True)
    assert ota_cycle._board_absent() is False             # a port is not absent either


def test_ykush_hidraw_finds_the_xs_by_hid_id(tmp_path, monkeypatch):
    for name, hid in (("hidraw0", "HID_ID=0003:0000046D:0000C52B"),   # a mouse
                      ("hidraw1", ota_cycle._YKUSH_XS_HID),
                      ("hidraw2", None)):                              # unreadable uevent
        d = tmp_path / name / "device"
        d.mkdir(parents=True)
        if hid:
            (d / "uevent").write_text("HID_NAME=x\n%s\n" % hid)
    monkeypatch.setattr(ota_cycle.glob, "glob", lambda pat: [str(tmp_path / n)
                                                            for n in ("hidraw0", "hidraw1", "hidraw2")])
    assert ota_cycle._ykush_hidraw() == "/dev/hidraw1"


def test_ykush_hidraw_is_none_without_the_hub(monkeypatch):
    monkeypatch.setattr(ota_cycle.glob, "glob", lambda pat: [])
    assert ota_cycle._ykush_hidraw() is None


def test_ykush_power_cycle_is_a_no_op_on_a_node_without_one(monkeypatch):
    monkeypatch.setattr(ota_cycle, "_ykush_hidraw", lambda: None)
    monkeypatch.setattr(ota_cycle.os, "open", lambda *a: (_ for _ in ()).throw(AssertionError("no HID")))
    assert ota_cycle.ykush_power_cycle("OPENMV_N6") is False


def test_ykush_power_cycle_sends_down_then_up_as_64_byte_reports(monkeypatch):
    writes, sleeps, logs = [], [], []
    monkeypatch.setattr(ota_cycle, "_ykush_hidraw", lambda: "/dev/hidraw7")
    monkeypatch.setattr(ota_cycle.os, "open", lambda path, flags: writes.append(("open", path)) or 42)
    monkeypatch.setattr(ota_cycle.os, "write", lambda fd, data: writes.append((fd, data)) or len(data))
    monkeypatch.setattr(ota_cycle.os, "close", lambda fd: writes.append(("close", fd)))
    monkeypatch.setattr(ota_cycle.time, "sleep", sleeps.append)
    monkeypatch.setattr(ota_cycle, "log", logs.append)
    assert ota_cycle.ykush_power_cycle("OPENMV_N6", off_s=3, settle_s=12) is True
    reports = [d for fd, d in writes if fd == 42]
    assert [len(r) for r in reports] == [65, 65]           # report id + 64 bytes, twice
    assert reports[0][:3] == bytes([0, 0x01, 0x01]) and reports[1][:3] == bytes([0, 0x11, 0x11])
    assert writes[0] == ("open", "/dev/hidraw7") and ("close", 42) in writes
    assert sleeps == [3, 12]                              # off, then let it enumerate
    assert any("VBUS power-cycle" in m for m in logs)


def test_ensure_cdc_revives_an_absent_board_through_the_ykush_before_any_pulse(monkeypatch):
    """No port, no DFU, a YKUSH present: VBUS cycle + wait for the boot, then re-probe; the J-Link
    pulse is never reached."""
    events = []
    answers = iter([False, True])
    monkeypatch.setattr(ota_cycle, "_cdc_answers", lambda: next(answers))
    monkeypatch.setattr(ota_cycle, "sh", lambda cmd, **k: (0, ""))
    monkeypatch.setattr(ota_cycle, "_board_absent", lambda: True)
    monkeypatch.setattr(ota_cycle, "ykush_power_cycle", lambda b: events.append("ykush") or True)
    monkeypatch.setattr(ota_cycle, "_await_boot", lambda b, **k: events.append("boot") or True)
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse", lambda b: events.append("pulse"))
    monkeypatch.setattr(ota_cycle, "log", lambda m: None)
    ota_cycle._ensure_cdc("OPENMV_N6")
    assert events == ["ykush", "boot"]


def test_ensure_cdc_pulses_when_the_board_is_present_but_mute(monkeypatch):
    events = []
    answers = iter([False, True])
    monkeypatch.setattr(ota_cycle, "_cdc_answers", lambda: next(answers))
    monkeypatch.setattr(ota_cycle, "sh", lambda cmd, **k: (0, ""))
    monkeypatch.setattr(ota_cycle, "_board_absent", lambda: False)
    monkeypatch.setattr(ota_cycle, "ykush_power_cycle", lambda b: events.append("ykush") or True)
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse", lambda b: events.append("pulse"))
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    monkeypatch.setattr(ota_cycle, "log", lambda m: None)
    ota_cycle._ensure_cdc("OPENMV_N6")
    assert events == ["pulse"]
