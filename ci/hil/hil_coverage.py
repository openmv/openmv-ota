#!/usr/bin/env python3
"""Device-path coverage from the HIL traces.

The device code (boot.py, the installer, the openmv_ota runtime) is ``# pragma: no cover`` in
the host suite: it needs MicroPython + real flash, so coverage.py can never touch it, and the
100% host gate deliberately excludes it. But those lines DO run on the bench -- and the OTA
code logs at every path it takes, so a HIL run leaves a record of exactly which device lines
executed (see ota_cycle.COVERAGE: each marker is a log line the device emits on a given path).

This tool closes the loop: it maps every coverage marker back to the source line that emits
it, reads the scenario traces, and reports which of those otherwise-uncoverable device lines
the live hardware actually executed -- and which scenario(s) hit each. It writes:

  * a human summary (stdout / --md): covered vs. not, by file, with the scenarios that hit each;
  * an lcov file (--lcov): the marker lines with hit counts, so the device paths can be folded
    into a combined coverage view (genhtml, or `coverage`-adjacent tooling) alongside the host
    report -- coverage on the lines unit tests structurally cannot reach.

It is NOT a line-by-line profiler (the device can't run coverage.py); it is branch-point
coverage keyed on the OTA code's own log statements -- the same markers the per-run gate
already checks -- aggregated across every scenario trace. A marker with no source match, or a
device path no scenario exercises, is called out so the catalog and the log lines can't drift
apart silently.

THE BOOT STUB IS READ DIFFERENTLY, and more completely. It is C, it logs "stub: <line>" on the
same UART at every decision it takes, and its log calls are whole literals -- so its source IS
the list of coverage points and needs no catalog. stub_line_coverage() matches every line the
stub can print against each trace's raw UART (the scored window and the provisioning boots
before it); the report's "Boot stub, line by line" section gives the lines a scenario can reach
and how many were seen, names the ones never seen, names the boards that never showed a line
of the shared code the others did, and flags any line only a hardware fault prints
(ota_cycle.STUB_FAULT_ONLY) that appeared anyway.

    python3 ci/hil/hil_coverage.py --traces <dir-of-hil-*.json> [--md out.md] [--lcov out.info]
"""

import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
DEVICE = os.path.join(REPO, "src/openmv_ota/build/device")

# The device modules whose log lines are the markers. openmv_rtc.py emits the clock-path
# markers and openmv_log.py emits the one bootstrap marker ("log: configured", the first line
# once its own handler is live -- so it can witness _configure()); both carry real emitting call
# sites, not just docstring examples, so both are scanned.
DEVICE_FILES = [
    os.path.join(DEVICE, "boot.py"),
    os.path.join(DEVICE, "openmv_ota/__init__.py"),
    os.path.join(DEVICE, "openmv_ota/data/installer.py"),
    os.path.join(DEVICE, "openmv_rtc.py"),
    os.path.join(DEVICE, "openmv_log.py"),
    os.path.join(DEVICE, "openmv_netcfg.py"),
    os.path.join(DEVICE, "openmv_recovery.py"),
    # openmv_wdt emits one marker -- "wdt: armed <window>", the witness that a watchdog is
    # REALLY running. The watchdog scenario requires it, because "the install survived" only
    # implies the watchdog behaved IF one was armed at all.
    os.path.join(DEVICE, "openmv_wdt.py"),
]
_LOGCALL = re.compile(r"\.(?:info|debug|warning|error)\(")

# The boot stub (C) logs on the same UART before the firmware starts; its markers map back
# to these sources. log_line("x") and log_kv("x", v) print "stub: x" (a kv's value follows
# at run time), log_str("x") prints x as it is -- the literals are whole lines by design,
# so a marker substring is checked against them verbatim like the Python ones.
STUB_DIR = os.path.join(DEVICE, "stub")
STUB_FILES = sorted(glob.glob(os.path.join(STUB_DIR, "common", "*.c"))
                    + glob.glob(os.path.join(STUB_DIR, "ports", "*", "*.c")))
_CLOGCALL = re.compile(r'\blog_(line|kv|str)\(\s*"((?:[^"\\]|\\.)*)"')


def stub_points():
    """(relpath, lineno, kind, text) for every log call in the stub: ``kind`` is how the C
    prints it (``line``: the literal alone; ``kv``: the literal and a 0x%08x value; ``str``:
    the literal as it is, a piece of a line) and ``text`` what the UART then carries."""
    for path in STUB_FILES:
        try:
            f = open(path)
        except OSError:
            continue
        with f:
            for lineno, line in enumerate(f, 1):
                m = _CLOGCALL.search(line)
                if not m:
                    continue
                kind, lit = m.group(1), m.group(2).encode().decode("unicode_escape")
                yield os.path.relpath(path, REPO), lineno, kind, (lit if kind == "str" else "stub: " + lit)


def stub_log_lines():
    """(relpath, lineno, text) for every line the stub can put on the UART, as the UART sees
    it."""
    for rel, lineno, _kind, text in stub_points():
        yield rel, lineno, text


def _stub_line_rx(kind, text):
    """How one stub log call reads in a captured UART line. A ``line`` is the whole line (so
    "stub: slot" never passes for "stub: slot ok"), a ``kv`` carries its value, a ``str`` is a
    fragment. Searched, not matched: a capture can run two lines together."""
    text = text.strip()
    if kind == "line":
        return re.compile(re.escape(text) + r"\s*$")
    if kind == "kv":
        return re.compile(re.escape(text) + r" 0x[0-9a-f]{8}")
    return re.compile(re.escape(text))


def stub_line_coverage(traces):
    """The boot stub's coverage, read off its own UART output: for every line its C can print,
    the scenarios whose captured UART shows it. ``{text: {"where": [(relpath, lineno)], "hits":
    {"board/network/scenario"}}}``. Unlike the markers above this needs no catalog: the stub
    logs at every decision it takes, so its source IS the list of points, and a trace's raw
    UART (the scored window, and the provisioning boots before it) says which were reached."""
    points = {}
    for rel, lineno, kind, text in stub_points():
        key = text.strip()
        if not key or key == "stub:":            # a bare newline, or the prefix of a built-up line
            continue
        p = points.setdefault(key, {"where": [], "hits": set(), "rx": _stub_line_rx(kind, text)})
        p["where"].append((rel, lineno))
    for t in traces:
        lines = t.get("log") or []
        if not lines:
            continue
        who = "%s/%s/%s" % (t["board"], t["network"], t["scenario"])
        for p in points.values():
            if any(p["rx"].search(ln) for ln in lines):
                p["hits"].add(who)
    for p in points.values():
        del p["rx"]
    return points


def _import_coverage_map():
    """{marker_substring: marker_id} from ota_cycle -- the single source of truth so this tool
    and the harness gate can never disagree on what a marker is."""
    sys.path.insert(0, HERE)
    os.environ.setdefault("WIFI_SSID", "")
    os.environ.setdefault("WIFI_PASSWORD", "")
    import ota_cycle
    return ota_cycle.COVERAGE


def _prefixes(s):
    """Progressively shorter word-boundary prefixes of a marker, longest first -- so a runtime
    marker ('boot: mounted A') falls back to the literal head of its format string
    ('boot: mounted %s' -> matches on 'boot: mounted')."""
    words = s.split(" ")
    for i in range(len(words), 0, -1):
        yield " ".join(words[:i])


def find_source(marker):
    """(relpath, lineno) of the log CALL that emits ``marker``, or (None, None). Matches the
    longest literal prefix of the marker that appears on a logging call line -- a Python
    device module's, or one of the boot stub's (C)."""
    for pref in _prefixes(marker):
        for path in DEVICE_FILES:
            try:
                with open(path) as f:
                    for lineno, line in enumerate(f, 1):
                        if pref in line and _LOGCALL.search(line):
                            return os.path.relpath(path, REPO), lineno
            except OSError:
                continue
        for rel, lineno, text in stub_log_lines():
            if pref in text:
                return rel, lineno
    return None, None


def load_traces(trace_dir):
    """Every hil-*.json trace: (scenario, board, network, passed, set(markers))."""
    out = []
    for p in sorted(glob.glob(os.path.join(trace_dir, "*.json"))):
        try:
            t = json.load(open(p))
        except (OSError, ValueError):
            continue
        if "markers" not in t:
            continue
        out.append({
            "file": os.path.basename(p),
            "scenario": t.get("scenario", "?"),
            "board": t.get("board", "?"),
            "network": t.get("network", "?"),
            "passed": t.get("passed", False),
            "markers": set(t.get("markers", [])),
            # the device's raw UART: the provisioning boots, then the scored window
            "log": list(t.get("log_setup") or []) + list(t.get("log") or []),
        })
    return out


def build(trace_dir):
    """Correlate markers -> source lines -> the scenarios/traces that hit them."""
    cov = _import_coverage_map()                     # {substring: marker_id}
    traces = load_traces(trace_dir)
    # marker_id -> (substring, relpath, lineno)
    loc = {}
    for sub, mid in cov.items():
        rel, ln = find_source(sub)
        # keep the first (a marker_id can have >1 substring; the XIP/block-device pairs do)
        loc.setdefault(mid, (sub, rel, ln))
    # marker_id -> {scenarios that hit it}
    hit = {}
    for t in traces:
        for mid in t["markers"]:
            hit.setdefault(mid, set()).add("%s/%s/%s" % (t["board"], t["network"], t["scenario"]))
    return cov, traces, loc, hit


def render_md(cov, traces, loc, hit):
    lines = ["# HIL device-path coverage", ""]
    lines.append("Device code is `# pragma: no cover` in the host suite (needs real hardware). "
                 "These are the paths the **live bench** executed, keyed on the OTA code's own "
                 "log markers, aggregated across every scenario trace.")
    lines.append("")
    ids = sorted(set(cov.values()))
    covered = [m for m in ids if hit.get(m)]
    lines.append("**%d / %d** device markers covered by HIL across %d trace(s)."
                 % (len(covered), len(ids), len(traces)))
    lines.append("")
    # group by file
    by_file = {}
    for mid in ids:
        _sub, rel, ln = loc.get(mid, (None, None, None))
        by_file.setdefault(rel, []).append((ln or 0, mid))
    lines.append("| marker | source | HIL-covered by |")
    lines.append("|---|---|---|")
    for rel in sorted(by_file, key=lambda r: (r is None, r or "")):
        for ln, mid in sorted(by_file[rel]):
            where = "%s:%d" % (rel, ln) if rel else "_(no source match)_"
            who = ", ".join(sorted(hit.get(mid, []))) or "**— not covered —**"
            lines.append("| `%s` | %s | %s |" % (mid, where, who))
    lines.append("")
    miss = [m for m in ids if not hit.get(m)]
    if miss:
        lines.append("Not exercised by any trace: " + ", ".join("`%s`" % m for m in sorted(miss)))
    lines.append("")
    lines += _render_stub_md(traces)
    lines.append("## Traces")
    for t in traces:
        lines.append("- `%s` %s/%s/%s -> %s (%d markers)"
                     % (t["file"], t["board"], t["network"], t["scenario"],
                        "PASS" if t["passed"] else "FAIL", len(t["markers"])))
    return "\n".join(lines) + "\n"


def _render_stub_md(traces):
    """The stub's line-by-line section: what its UART showed, what it never showed, and of the
    latter which lines only a hardware fault can print (ota_cycle.STUB_FAULT_ONLY, each with
    its reason) -- so what is left is the list of paths a scenario could reach and none does."""
    points = stub_line_coverage(traces)
    if not points:
        return []
    sys.path.insert(0, HERE)
    import ota_cycle
    fault = ota_cycle.STUB_FAULT_ONLY

    def is_fault(text):
        return any(text.startswith(f) for f in fault)

    seen = sorted(t for t, p in points.items() if p["hits"])
    reach = sorted(t for t in points if not is_fault(t))
    covered = [t for t in reach if points[t]["hits"]]
    out = ["## Boot stub, line by line", ""]
    out.append("Read off the stub's own UART output: every line its C can print is a point, and "
               "a trace's captured UART (the provisioning boots and the scored window) says which "
               "were reached.")
    out.append("")
    out.append("**%d / %d** lines a scenario can reach were seen (%d lines in the source; %d need "
               "a hardware fault)." % (len(covered), len(reach), len(points), len(points) - len(reach)))
    out.append("")
    missing = [t for t in reach if not points[t]["hits"]]
    if missing:
        out.append("Reachable, never seen:")
        out.append("")
        for t in missing:
            out.append("- `%s` (%s)" % (t, ", ".join("%s:%d" % w for w in points[t]["where"])))
        out.append("")
    # A line of the shared code that some stub boards showed and others never did: the same
    # path, unexercised on that board.
    stub_boards = sorted({h.split("/")[0] for h in points.get("stub: version", {"hits": ()})["hits"]})
    gaps = []
    for t in covered:
        if not all("/common/" in r for r, _n in points[t]["where"]):
            continue                             # a port's own line: not every board has it
        want = [b for b in stub_boards
                if not t.startswith(ota_cycle.STUB_CRYPT_LINES) or ota_cycle._stub_encrypts(b)]
        lack = sorted(set(want) - {h.split("/")[0] for h in points[t]["hits"]})
        if lack:
            gaps.append("- `%s`: never on %s" % (t, ", ".join(lack)))
    if gaps:
        out.append("Seen on some stub boards, not on others (%d stub boards in these traces):"
                   % len(stub_boards))
        out.append("")
        out += gaps
        out.append("")
    bad = [t for t in seen if is_fault(t)]
    if bad:
        out.append("**Fault lines that DID appear** (a board hit a hardware fault path):")
        out.append("")
        for t in bad:
            out.append("- `%s`: %s" % (t, ", ".join(sorted(points[t]["hits"]))))
        out.append("")
    out.append("| stub line | source | boards | scenarios |")
    out.append("|---|---|---|---|")
    for t in sorted(points):
        hits = points[t]["hits"]
        boards = sorted({h.split("/")[0] for h in hits})
        scen = sorted({h.split("/")[2] for h in hits})
        where = ", ".join("%s:%d" % (os.path.basename(r), n) for r, n in points[t]["where"])
        if hits:
            out.append("| `%s` | %s | %s | %s |" % (t, where, ", ".join(boards), ", ".join(scen)))
        else:
            why = next((fault[f] for f in fault if t.startswith(f)), None)
            out.append("| `%s` | %s | %s | |" % (t, where, ("_fault only: %s_" % why) if why else "**— not seen —**"))
    out.append("")
    return out


def render_lcov(loc, hit):
    """lcov, DOMINATOR-BACK-FILLED: for each marker that actually FIRED in some trace, credit
    not just the marker's own line but every line that DOMINATES it -- the lines that provably
    ran to reach it (static_coverage). A marker firing on hardware is proof those lines ran, so
    this is sound, and turns "16 marker lines" into the whole proven path per file. Foldable
    next to the host coverage (genhtml / Codecov flag). Falls back to marker-lines-only if the
    static analyzer or coverage.py isn't importable."""
    fired = {}                                   # rel -> {marker line: hit count}
    for mid, (_sub, rel, ln) in loc.items():
        n = len(hit.get(mid, []))
        if rel and ln and n:                     # only markers that fired in >=1 trace
            fired.setdefault(rel, {})[ln] = max(fired.get(rel, {}).get(ln, 0), n)
    try:
        sys.path.insert(0, HERE)
        import static_coverage
    except Exception:
        static_coverage = None
    out = []
    for rel in sorted(fired):
        marks = fired[rel]
        cnt = max(marks.values())                # coarse per-file hit count (lcov only needs >0)
        lines = set(marks)
        if static_coverage is not None:
            try:
                lines |= static_coverage.provable_lines(os.path.join(REPO, rel), list(marks))
            except Exception:
                pass                             # keep marker lines if the file can't be analyzed
        out.append("TN:")
        out.append("SF:" + rel)
        for ln in sorted(lines):
            out.append("DA:%d,%d" % (ln, cnt))
        out.append("LF:%d" % len(lines))
        out.append("LH:%d" % len(lines))
        out.append("end_of_record")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", default=".", help="directory of hil-*.json scenario traces")
    ap.add_argument("--md", help="write the markdown summary here (else stdout)")
    ap.add_argument("--lcov", help="write an lcov .info file here")
    args = ap.parse_args()

    cov, traces, loc, hit = build(args.traces)
    md = render_md(cov, traces, loc, hit)
    if args.md:
        open(args.md, "w").write(md)
        print("wrote " + args.md)
    else:
        print(md)
    if args.lcov:
        open(args.lcov, "w").write(render_lcov(loc, hit))
        print("wrote " + args.lcov)
    return 0


if __name__ == "__main__":
    sys.exit(main())
