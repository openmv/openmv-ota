#!/usr/bin/env python3
"""OTA efficiency metrics from the HIL traces -- the REAL cost of an update, measured on hardware
every run.

The headline is the DELTA saving: a delta transfers only a few percent of a full image, so an update
is KiB + seconds, not MiB + minutes. Posting it on every real-hardware run makes bandwidth efficiency
a TRACKED, defensible number -- a selling point backed by measurement, not a claim.

Each per-scenario trace carries ``metrics`` (the published artifact sizes: manifest / full img.gz /
delta.gz / uncompressed payload) and ``phases`` (per-phase wall-time, incl. ``install``). This folds
them per board and writes a markdown table (-> the run summary) + a machine-readable metrics.json.

    python3 ci/hil/ota_metrics.py --traces <dir> [--md out.md] [--json out.json]
"""
import argparse
import glob
import json
import os


def human(n):
    if not n:
        return "-"
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KiB" % (n / 1024)
    return "%.2f MiB" % (n / (1024 * 1024))


def fold(trace_dir):
    """board -> {manifest, full_img_gz, delta_gz, payload, install_s}. A delta scenario publishes
    both full+delta; keep the SMALLEST delta seen (the best real number) and the payload/full from
    whichever scenario carried them."""
    by = {}
    for fn in sorted(glob.glob(os.path.join(trace_dir, "*.json"))):
        try:
            t = json.load(open(fn))
        except (ValueError, OSError):
            continue
        m = t.get("metrics") or {}
        if not m:
            continue
        agg = by.setdefault(t.get("board", "?"), {})
        for k, v in m.items():
            if not v:
                continue
            if k == "delta_gz":
                agg[k] = min(agg.get(k, v), v)          # best (smallest) delta
            else:
                agg.setdefault(k, v)
        install = (t.get("phases") or {}).get("install")
        if install and t.get("scenario") == "delta":    # the happy-path install time
            agg["install_s"] = install
    return by


def report(by):
    if not by:
        return "### OTA efficiency\n\n_No size metrics in this run's traces._\n"
    lines = ["### OTA efficiency (measured on real hardware)", ""]
    lines.append("| board | manifest | full image | **delta** | **delta vs full** | payload (flash) | "
                 "install |")
    lines.append("|---|---|---|---|---|---|---|")
    for board in sorted(by):
        a = by[board]
        full, delta, payload = a.get("full_img_gz"), a.get("delta_gz"), a.get("payload")
        ratio = ("**%.1f%%** (%.0f× less)" % (100.0 * delta / full, full / delta)
                 if full and delta else "-")
        inst = ("%.0f s" % a["install_s"]) if a.get("install_s") else "-"
        pay = (human(payload) + (" (%.0f× gz)" % (payload / full) if payload and full else ""))
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            board, human(a.get("manifest")), human(full), human(delta), ratio, pay, inst))
    lines += ["",
              "_The **delta** is the whole point: a routine update ships only the changed bytes, so "
              "devices pull KiB not MiB. `manifest` is the signed metadata fetched per install; "
              "`payload` is the uncompressed image written to flash._"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", required=True)
    ap.add_argument("--md")
    ap.add_argument("--json", dest="js")
    args = ap.parse_args()
    by = fold(args.traces)
    md = report(by)
    print(md)
    if args.md:
        with open(args.md, "w") as f:
            f.write(md)
    if args.js:
        with open(args.js, "w") as f:
            json.dump(by, f, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
