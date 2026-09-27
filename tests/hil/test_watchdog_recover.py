"""watchdog_recover (ci/hil/ota_cycle.py): an armed watchdog must survive run() rebuilding the
network. The app points run() at a closed port so recover= fires; a bite lands mid-hook, so
`run.recovered` is only ever logged by a board that survived."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "ci", "hil"))

import ota_cycle  # noqa: E402

def test_wdt_recover_app_arms_the_watchdog_and_aims_run_at_a_closed_port(monkeypatch):
    monkeypatch.setitem(ota_cycle.CFG, "server", "https://192.168.0.100:8443")
    app = ota_cycle.bench_main_py("OPENMV4P", "wifi", "wdt_recover")
    assert "openmv_wdt.start()" in app and "openmv_wdt.feed()" in app
    assert "'https://192.168.0.100:9'" in app, "every check-in must fail so recover= fires"
    assert "recover=_bring_up" in app
    assert ":8443" not in app.split("openmv_ota.run(")[1].split(")")[0]


def test_watchdog_recover_is_scored_on_markers_and_runs_where_watchdog_does():
    sc = ota_cycle.SCENARIOS["watchdog_recover"]
    assert sc["by_marker"] is True and sc["publish"] == "none"
    # a bite lands mid-hook, so run.recovered can only come from a board that survived
    assert {"wdt.armed", "run.recover", "run.recovered"} <= set(sc["expect"])
    for board, spec in ota_cycle.BOARDS.items():
        if spec["network"] == "file":
            continue
        scs = ota_cycle.regression_scenarios(board, spec["network"])
        assert ("watchdog" in scs) == ("watchdog_recover" in scs), board


def test_no_slot_never_inherits_an_armed_watchdog_app():
    """no_slot has no golden flash: it seeds its marker over the REPL on the previous scenario's
    app. After an armed-watchdog app that seed gets bitten (RT1060, PR #91 gate), so no_slot must
    run BEFORE the watchdog group."""
    for board, spec in ota_cycle.BOARDS.items():
        if spec["network"] == "file":
            continue
        scs = ota_cycle.regression_scenarios(board, spec["network"])
        if "no_slot" in scs:
            prev = scs[scs.index("no_slot") - 1]
            assert ota_cycle.SCENARIOS[prev]["app"] not in ("wdt", "wdt_bite", "wdt_recover"), (
                board, prev)
