"""Guard the HIL coverage checklist against drift -- at PR time, not on the bench.

``ci/hil/ota_cycle.COVERAGE`` maps device LOG SUBSTRINGS -> markers, and the HIL gate keys on
those exact substrings. So a reworded or removed device log line would silently stop a marker
from ever firing, breaking the next (manual, hardware) bench run with no earlier signal -- the
one real brittleness of a log-line-as-coverage design. These host tests move that failure
here, where it costs nothing:

  * every ``COVERAGE`` substring still appears VERBATIM on a logging call in the device source
    (a trailing runtime slot name -- the ``%s`` in ``"boot: mounted %s"`` -- is dropped first,
    since that word is substituted at runtime and can't be in the source literal);
  * every marker is an expected path of SOME scenario (no dead coverage points), and no
    scenario's ``expect`` and ``forbid`` sets overlap.

``ci/hil`` is not part of the coverage source (pyproject ``source = ["openmv_ota"]``), so
importing it here does not touch the 100% gate.
"""

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CIHIL = os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil"))
sys.path.insert(0, _CIHIL)
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import ota_cycle       # noqa: E402  (ci/hil, added to sys.path above)
import hil_coverage    # noqa: E402

_LOGCALL = re.compile(r"\.(?:info|debug|warning|error)\(")
# Slot names substituted into "boot: mounted %s" / "-> mounted %s" at runtime; every other
# marker is a plain literal chosen to appear verbatim in the source.
#
# This escape hatch is why the A/B rename could go stale silently: a COVERAGE key ending in a
# runtime-substituted word is checked against the source with that word STRIPPED, so
# "boot: mounted FRONT" kept matching `log.info("boot: mounted %s ...")` long after the device
# had stopped ever printing the word FRONT. The v2 keys avoid the trap by not naming a slot at
# all ("boot: mounted", "boot: rejected"), which is why the set below is now empty -- keep it
# that way, and prefer a key that stops before the %s over one that guesses what fills it.
_RUNTIME_TAIL: set[str] = set()


def _stable_literal(substr):
    words = substr.split()
    if words and words[-1] in _RUNTIME_TAIL:
        return " ".join(words[:-1])
    return substr


def _device_log_lines():
    for path in hil_coverage.DEVICE_FILES:
        with open(path) as f:
            for line in f:
                if _LOGCALL.search(line):
                    yield line
    # A few markers are emitted by the BENCH APPS (built in ota_cycle.bench_main_py), not the
    # shipped device code -- e.g. the watchdog-bite witnesses. Validate those substrings against
    # their real emission site too: the app's own `_blog.<level>(...)` calls in ota_cycle.py.
    with open(ota_cycle.__file__) as f:
        for line in f:
            if "_blog." in line and _LOGCALL.search(line):
                yield line


def test_every_coverage_substring_is_a_live_device_log_line():
    lines = list(_device_log_lines())
    drifted = [sub for sub in ota_cycle.COVERAGE
               if not any(_stable_literal(sub) in ln for ln in lines)]
    assert not drifted, (
        "COVERAGE substrings no longer emitted verbatim by any device log call -- the log "
        "wording drifted. Update ci/hil/ota_cycle.COVERAGE (and any scenario expect/forbid "
        "sets) to match the new wording: %r" % drifted)


def test_every_marker_is_expected_by_some_scenario():
    expected = set()
    for name in ota_cycle.SCENARIOS:
        for board in ota_cycle.BOARDS:
            expected |= ota_cycle.scenario_markers(board, name)[0]
    dead = sorted(set(ota_cycle.COVERAGE.values()) - expected)
    assert not dead, (
        "markers no scenario expects -- dead coverage points (add them to a scenario's expect "
        "set, or drop them from COVERAGE): %r" % dead)


def test_scenario_expect_and_forbid_are_disjoint():
    for name in ota_cycle.SCENARIOS:
        for board in ota_cycle.BOARDS:
            expect, forbid = ota_cycle.scenario_markers(board, name)
            overlap = sorted(expect & forbid)
            assert not overlap, "%s/%s expect & forbid overlap: %r" % (name, board, overlap)


def test_regression_scenarios_are_valid_and_board_gated():
    for board in ota_cycle.BOARDS:
        primary = ota_cycle.BOARDS[board]["network"]
        if primary == "file":
            # Classic boards have ONE transport and one regression -- the file list -- whatever
            # interface is asked for (the matrix never generates a secondary leg for them, and
            # their builds could not run a network scenario anyway: no TLS stack).
            scs = ota_cycle.regression_scenarios(board, "file")
            assert scs == ["file_full", "file_bad_sig"]
            assert all(s in ota_cycle.SCENARIOS for s in scs)
            continue
        for net in ("lan", "wifi"):
            scs = ota_cycle.regression_scenarios(board, net)
            assert scs, "%s/%s regression is empty" % (board, net)
            assert all(s in ota_cycle.SCENARIOS for s in scs), \
                "%s/%s has an unknown scenario: %r" % (board, net, scs)
            # coproc is AE3-only, on its primary interface, and only when opted in via
            # COPROC_ENABLED -- its MRAM write currently wedges the AE3, so it's out of the
            # default regression (the rest of the suite is safe: normal sync() skips the partition).
            assert any(s.startswith("coproc") for s in scs) == \
                (board == "OPENMV_AE3" and net == primary and ota_cycle.COPROC_ENABLED)
            # no_slot only on block-device boards (blhost slot-erase)
            if "no_slot" in scs:
                assert ota_cycle.BOARDS[board]["flash"] == "blhost_imx"
            # a secondary interface just proves the network path
            if net != primary:
                assert scs == ["delta"]
    # every board's PRIMARY-interface regression, unioned, covers the tamper + happy + rollback set
    union = set()
    for board in ota_cycle.BOARDS:
        union |= set(ota_cycle.regression_scenarios(board, ota_cycle.BOARDS[board]["network"]))
    assert union >= {"delta", "full", "rollback", "corrupt", "bad_sig", "bad_key", "bad_version"}


def test_coproc_opt_in_readds_it_to_the_ae3_regression(monkeypatch):
    """coproc/coproc_skip are gated OUT by default (COPROC_ENABLED=False) so the AE3 stops re-bricking
    on the coprocessor-MRAM write, but the scenarios still exist and HIL_COPROC=1 re-adds them to the
    AE3's primary-interface regression for a manual coproc run once that write is fixed."""
    ae3, primary = "OPENMV_AE3", ota_cycle.BOARDS["OPENMV_AE3"]["network"]
    monkeypatch.setattr(ota_cycle, "COPROC_ENABLED", False)
    assert not any(s.startswith("coproc") for s in ota_cycle.regression_scenarios(ae3, primary))
    monkeypatch.setattr(ota_cycle, "COPROC_ENABLED", True)
    reenabled = ota_cycle.regression_scenarios(ae3, primary)
    assert "coproc" in reenabled and "coproc_skip" in reenabled
