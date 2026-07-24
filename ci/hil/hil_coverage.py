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

# The device modules whose log lines are the markers. openmv_log.py is deliberately excluded:
# it only holds DOCSTRING examples of the log format, not the emitting call sites.
DEVICE_FILES = [
    os.path.join(DEVICE, "boot.py"),
    os.path.join(DEVICE, "openmv_ota/__init__.py"),
    os.path.join(DEVICE, "openmv_ota/data/installer.py"),
]
_LOGCALL = re.compile(r"\.(?:info|debug|warning|error)\(")


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
    marker ('boot: mounted FRONT') falls back to the literal head of its format string
    ('boot: mounted %s' -> matches on 'boot: mounted')."""
    words = s.split(" ")
    for i in range(len(words), 0, -1):
        yield " ".join(words[:i])


def find_source(marker):
    """(relpath, lineno) of the log CALL that emits ``marker``, or (None, None). Matches the
    longest literal prefix of the marker that appears on a logging call line."""
    for pref in _prefixes(marker):
        for path in DEVICE_FILES:
            try:
                with open(path) as f:
                    for lineno, line in enumerate(f, 1):
                        if pref in line and _LOGCALL.search(line):
                            return os.path.relpath(path, REPO), lineno
            except OSError:
                continue
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
        # keep the first (a marker_id can have >1 substring; boot.mount.front/back share a line)
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
    lines.append("## Traces")
    for t in traces:
        lines.append("- `%s` %s/%s/%s -> %s (%d markers)"
                     % (t["file"], t["board"], t["network"], t["scenario"],
                        "PASS" if t["passed"] else "FAIL", len(t["markers"])))
    return "\n".join(lines) + "\n"


def render_lcov(loc, hit):
    """Minimal lcov: one record per device file, DA lines for each marker's source line with a
    hit count = number of distinct board/net/scenario runs that executed it. Foldable into a
    combined report (genhtml, lcov merge) next to the host coverage."""
    by_file = {}
    for mid, (_sub, rel, ln) in loc.items():
        if rel and ln:
            by_file.setdefault(rel, {})
            by_file[rel][ln] = max(by_file[rel].get(ln, 0), len(hit.get(mid, [])))
    out = []
    for rel in sorted(by_file):
        out.append("TN:")
        out.append("SF:" + rel)
        da = by_file[rel]
        for ln in sorted(da):
            out.append("DA:%d,%d" % (ln, da[ln]))
        out.append("LF:%d" % len(da))
        out.append("LH:%d" % sum(1 for ln in da if da[ln] > 0))
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
