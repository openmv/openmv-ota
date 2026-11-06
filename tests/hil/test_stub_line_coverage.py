"""The boot stub's coverage is read off its own UART output: every log call in its C is a
point, and a trace's raw UART says which were reached (hil_coverage.stub_line_coverage). These
pin the reading -- a line is not credited for a longer one that starts the same way -- and keep
the two hand-kept lists it leans on (the lines only a hardware fault prints, the lines only an
encrypting stub prints) from drifting away from the C."""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "ci", "hil"))
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import hil_coverage  # noqa: E402
import ota_cycle  # noqa: E402


def _points():
    return {text.strip(): kind for _r, _l, kind, text in hil_coverage.stub_points() if text.strip()}


def _trace(board, scenario, log, setup=()):
    return {"board": board, "network": "lan", "scenario": scenario, "log": list(setup) + list(log)}


def test_every_log_call_is_a_point_with_how_it_prints():
    pts = _points()
    assert pts["stub: firmware area verified"] == "line"
    assert pts["stub: record matches"] == "kv"            # a value follows at run time
    assert pts["stub: reject"] == "str"                    # a piece of a line, as it is
    assert pts["--- stub boot ---"] == "str"
    # the same texts the marker map is checked against
    assert {t for _r, _l, t in hil_coverage.stub_log_lines()} >= {"stub: firmware area verified"}


def test_a_line_is_not_credited_for_a_longer_one():
    cov = hil_coverage.stub_line_coverage([_trace("OPENMV_N6", "delta", [
        "stub: slot ok",                                   # NOT the kv "stub: slot 0x..."
        "stub: boot trial",
        "stub: record matches 0x00000001",
        "stub: xspi kclk 0x0bebc200\x00junk xspi id 0x00c28039",   # two lines run together
    ])])
    assert cov["stub: slot ok"]["hits"] == {"OPENMV_N6/lan/delta"}
    assert not cov["stub: slot"]["hits"]                   # the value never appeared
    assert cov["stub: boot trial"]["hits"] and not cov["stub: boot normal"]["hits"]
    assert cov["stub: record matches"]["hits"]
    assert cov["stub: xspi kclk"]["hits"]                  # searched, so a run-on line still counts
    assert not cov["stub: xspi id"]["hits"]                # ...but only with its own prefix intact
    assert "stub:" not in cov and "" not in cov            # no point for a bare prefix or newline
    assert all(w for p in cov.values() for w in p["where"])


def test_the_provisioning_boots_count_and_come_from_the_trace(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({
        "board": "OPENMV_N6", "network": "lan", "scenario": "delta", "passed": True,
        "markers": ["stub.version"],
        "log_setup": ["stub: firmware area in the clear: encrypting it"],
        "log": ["stub: firmware area verified"]}))
    (tmp_path / "old.json").write_text(json.dumps({     # a trace from before the raw log was kept
        "board": "OPENMV4P", "network": "wifi", "scenario": "delta", "passed": True, "markers": []}))
    traces = hil_coverage.load_traces(str(tmp_path))
    cov = hil_coverage.stub_line_coverage(traces)
    assert cov["stub: firmware area in the clear: encrypting it"]["hits"] == {"OPENMV_N6/lan/delta"}
    assert cov["stub: firmware area verified"]["hits"] == {"OPENMV_N6/lan/delta"}


def test_the_report_names_what_no_scenario_reached_and_any_fault_that_showed():
    traces = [
        _trace("OPENMV_N6", "delta", ["stub: version 0x00000001", "stub: flash cipher on",
                                      "stub: boot normal", "stub: slot rejected",
                                      "stub: stray fault cfsr 0x00000400"]),
        _trace("OPENMV4P", "delta", ["stub: version 0x00000001", "stub: boot normal"]),
    ]
    md = "\n".join(hil_coverage._render_stub_md(traces))
    assert "## Boot stub, line by line" in md
    reach = md.split("Reachable, never seen:")[1].split("Seen on some stub boards")[0]
    assert "`stub: boot reinstalled`" in reach             # a path a scenario could exercise
    assert "`stub: no hardware key`" not in reach          # ...and one only a fault prints
    gaps = md.split("Seen on some stub boards")[1].split("**Fault lines")[0]
    assert "`stub: slot rejected`: never on OPENMV4P" in gaps
    assert "flash cipher" not in gaps                      # the 4P's stub does not encrypt
    assert "**Fault lines that DID appear**" in md and "`stub: stray fault cfsr`: OPENMV_N6/lan/delta" in md
    assert "_fault only: the chip's hardware key is not valid_" in md
    assert "**0 / " in "\n".join(hil_coverage._render_stub_md([]))     # no traces: nothing seen


def test_the_fault_only_and_encrypting_lists_name_live_lines():
    pts = _points()
    for prefix in ota_cycle.STUB_FAULT_ONLY:
        assert any(t.startswith(prefix) for t in pts), "no stub line starts with %r" % prefix
    for prefix in ota_cycle.STUB_CRYPT_LINES:
        assert any(t.startswith(prefix) for t in pts), "no stub line starts with %r" % prefix


def test_no_scenario_expects_a_line_only_a_fault_prints():
    fault_markers = {mid for sub, mid in ota_cycle.COVERAGE.items()
                     if any(sub.startswith(f) for f in ota_cycle.STUB_FAULT_ONLY)}
    assert fault_markers == {"stub.failed"}
    for name in ota_cycle.SCENARIOS:
        for board in ota_cycle.BOARDS:
            expect, _forbid = ota_cycle.scenario_markers(board, name)
            assert "stub.failed" not in expect, (board, name)


def test_a_fault_in_the_stub_is_forbidden_wherever_the_stub_runs(monkeypatch):
    monkeypatch.setattr(ota_cycle, "_has_stub", lambda b: b == "OPENMV_N6")
    for sub in ("stub: stray fault", "stub: fault pc", "stub: no hardware key",
                "stub: flash cipher setup failed", "stub: blob read failed"):
        assert ota_cycle.COVERAGE[sub] == "stub.failed"
    assert "stub.failed" in ota_cycle.scenario_markers("OPENMV_N6", "delta")[1]
