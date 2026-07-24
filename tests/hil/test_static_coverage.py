"""Regression guard for the static device-path coverage analyzer (ci/hil/static_coverage.py).

The analyzer's soundness hinges on modelling control flow correctly -- in particular the one
subtle case that bit during bring-up: coverage.py represents an ``except``/``finally`` as a
pseudo-entry with no in-arc, which (untreated) looks like a free entry that bypasses everything
before the ``try`` and wrongly un-dominates all post-try code. static_coverage adds
``try-entry -> handler`` edges to fix it. These tests pin that -- via an INVARIANT, not fragile
line numbers, so they survive edits to the device code:

    a marker's OWN function entry must dominate it -- you cannot reach the marker without
    entering its function -- so the function's first line must always be "provable".

If the exception modelling regresses, a marker in a try/except function loses its entry and
this fails. ci/hil is not in the coverage source, so importing it here doesn't touch the gate.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil")))
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import static_coverage  # noqa: E402


def _function_entry(owner, body):
    """First EXECUTABLE line of each function (coverage skips the docstring, so the AST body
    start isn't it) -- the line every path through the function passes first."""
    entry = {}
    for line in body:
        name = owner.get(line)
        if name and (name not in entry or line < entry[name]):
            entry[name] = line
    return entry


def test_every_marker_functions_entry_is_provable():
    # The invariant the exception-edge fix restores: a marker's function entry dominates it.
    for f in static_coverage.DEFAULT_FILES:
        r = static_coverage.analyze(f)
        entry = _function_entry(r["owner"], r["body"])
        for m in r["markers"]:
            fn = r["owner"].get(m)
            e = entry.get(fn)
            assert e in r["coverable"], (
                "%s: marker on line %d (function %s) -- its function entry line %d is NOT "
                "provable, so dominance is broken (try/except pseudo-entry not modelled?)"
                % (os.path.basename(f), m, fn, e))


def test_analyzer_finds_the_known_markers():
    # sanity: the three device modules together expose the OTA markers we key coverage on.
    found = set()
    for f in static_coverage.DEFAULT_FILES:
        r = static_coverage.analyze(f)
        found |= {r["src"][m - 1] for m in r["markers"]}
    assert any("no bootable slot" in ln for ln in found)          # boot.py
    assert any("installed + armed" in ln for ln in found)          # installer
    assert any("kept running FRONT" in ln for ln in found)         # runtime confirm


def test_coverable_is_a_subset_of_reachable_body():
    # never claim a non-executable / out-of-function line as covered.
    for f in static_coverage.DEFAULT_FILES:
        r = static_coverage.analyze(f)
        assert r["coverable"] <= r["body"]


def test_gaps_are_hil_only_never_unit_tested():
    # A gap must be a `# pragma: no cover` (HIL-only) line -- unit tests + HIL are additive, so
    # a unit-tested line is already covered and never a "add a print here" gap.
    for f in static_coverage.DEFAULT_FILES:
        r = static_coverage.analyze(f)
        assert set(r["gaps"]) <= r["hil_only"]
        assert not (set(r["gaps"]) & r["unit"])


def test_credit_is_intraprocedural_no_cross_function():
    # Dominance is intraprocedural: a marker's credited lines must all live in the marker's OWN
    # function. Guards against coverage.py arc-graph quirks (a for-loop body / except handler
    # that, at whole-file scope, made an UNRELATED function's line look like a dominator -- an
    # over-credit, the unsafe direction, which shipped once).
    for f in static_coverage.DEFAULT_FILES:
        r = static_coverage.analyze(f)
        for m in r["markers"]:
            fn = r["owner"].get(m)
            for line in static_coverage.provable_lines(f, [m]):
                assert r["owner"].get(line) == fn, (
                    "%s: marker on line %d (%s) credits line %d in a DIFFERENT function (%s) -- "
                    "cross-function over-credit" % (f, m, fn, line, r["owner"].get(line)))
