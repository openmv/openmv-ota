#!/usr/bin/env python3
"""Static device-path coverage: what the current log prints can PROVE, and where the gaps are.

The device can't run a coverage tracer (MicroPython, across reboots, installer execs from RAM),
so on-device coverage is a set of log prints we watch on the UART. This asks a purely static
question about that print set -- no device change, no execution:

    if a given source line runs, can any print PROVE it ran?

A print (marker) M, when we see it on the UART, proves every line that DOMINATES M ran -- the
lines on every path from the function entry to M. So the lines a print can witness = the union
of the dominators of every marker. Everything else is a GAP: a line no current print can ever
show executed -> exactly where to add a UART print.

Soundness runs the safe way: dominators only ever UNDERSTATE what's proven, so this only ever
OVERSTATES the gaps. It never says "covered" about something it can't prove -- the verification
posture (if it wasn't witnessed, treat it as untested). Intraprocedural: it proves lines within
a marker's own function; a marker reached through a call doesn't back-fill the caller (that
needs interprocedural analysis and dies at the reboot/RAM-exec seams anyway) -- those lines just
show as gaps until they get their own print.

The control-flow graph is coverage.py's OWN arc graph (the one it uses for branch coverage), so
async / generators / comprehensions are modelled correctly -- no third-party CFG builder.

    python3 ci/hil/static_coverage.py [file ...]      # default: the three device modules
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")
import ota_cycle  # noqa: E402  (the COVERAGE substrings)

DEVICE = os.path.join(_REPO, "src/openmv_ota/build/device")
DEFAULT_FILES = [
    os.path.join(DEVICE, "boot.py"),
    os.path.join(DEVICE, "openmv_ota/__init__.py"),
    os.path.join(DEVICE, "openmv_ota/data/installer.py"),
    os.path.join(DEVICE, "openmv_rtc.py"),
    os.path.join(DEVICE, "openmv_log.py"),
    os.path.join(DEVICE, "openmv_wdt.py"),
]
_LOGCALL = ("info(", "debug(", "warning(", "error(")


def _dominators(preds, nodes, root):
    """Classic iterative dominators. dom[n] = the nodes on every path root->n (incl. n)."""
    alln = set(nodes)
    dom = {n: (set([root]) if n == root else set(alln)) for n in nodes}
    changed = True
    while changed:
        changed = False
        for n in nodes:
            if n == root:
                continue
            ps = [p for p in preds.get(n, ()) if p in dom]
            new = set(alln)
            for p in ps:
                new &= dom[p]
            new = {n} | (new if ps else set())
            if new != dom[n]:
                dom[n] = new
                changed = True
    return dom


def _exception_edges(source):
    """coverage.py models an ``except``/``finally`` as a pseudo-entry with NO in-arc (an
    exception can fire anywhere in the try, so there is no single edge into the handler). Left
    alone it looks like a free entry -- a path that bypasses everything before the try, which
    wrongly un-dominates all post-try code. Reaching a handler DOES prove the try was entered,
    so add ``try-entry -> handler`` (and ``-> finally``) edges to restore correct dominance."""
    import ast
    edges = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Try):
            entry = node.body[0].lineno
            for h in node.handlers:
                edges.add((entry, h.lineno))                 # the `except` line = coverage's entry
            if node.finalbody:
                edges.add((entry, node.finalbody[0].lineno))
    return edges


def _dominators_for(filename):
    """(dom, stmts): dom[line] = the nodes that dominate it (must run to reach it), over
    coverage.py's arc graph with the except/finally pseudo-entries repaired; stmts = the
    executable lines. Shared by analyze() and provable_lines()."""
    from coverage.parser import PythonParser
    source = open(filename).read()
    p = PythonParser(filename=filename)
    p.parse_source()
    stmts = set(p.statements)
    arcs = set(p.arcs()) | _exception_edges(source)
    # Build the graph. Nodes include coverage's negative entry/exit sentinels. A synthetic ROOT
    # feeds every node with no real predecessor (the module start + each function's entry), so
    # dominance is computed per entry and functions don't falsely dominate one another.
    nodes = {a for arc in arcs for a in arc}
    preds = {}
    for a, b in arcs:
        preds.setdefault(b, set()).add(a)
    ROOT = 0
    while ROOT in nodes:
        ROOT -= 1
    nodes.add(ROOT)
    for n in list(nodes):
        if n != ROOT and not preds.get(n):
            preds.setdefault(n, set()).add(ROOT)
    order = [ROOT] + sorted(n for n in nodes if n != ROOT)   # any order converges to the fixpoint
    return _dominators(preds, order, ROOT), stmts


def provable_lines(filename, marker_lines):
    """The device lines PROVEN executed if the given marker lines fired: the union of their
    dominators (positive source lines) intersected with the executable set. Sound -- a marker
    firing means every line that dominates it ran.

    Dominance is INTRAPROCEDURAL by definition (a marker's dominators all live in its own
    function), so we constrain each marker's credit to its own function. That is not just a
    scoping nicety: coverage.py's arc graph can mis-model some intra-function control flow (a
    for-loop body whose entry arc it renders oddly, an except pseudo-entry), which at the whole
    -file level can make an UNRELATED function's line look like a dominator -- an over-credit,
    the unsafe direction. Clipping to the marker's own function makes the result robust to that
    regardless of graph quirks: we only ever credit lines in the function that emitted the print."""
    dom, stmts = _dominators_for(filename)
    owner = _func_owner(open(filename).read())
    out = set()
    for m in marker_lines:
        fn = owner.get(m)
        out |= {d for d in dom.get(m, ()) if d > 0 and owner.get(d) == fn}
    return out & stmts


def analyze(filename):
    from coverage.parser import PythonParser
    source = open(filename).read()
    p = PythonParser(filename=filename)
    p.parse_source()
    stmts = set(p.statements)                             # ALL executable device lines
    # Unit tests + HIL are ADDITIVE and cover disjoint sets by design: the pure logic is
    # host-unit-tested (in the 100% gate), and the real-I/O glue is `# pragma: no cover`
    # (sockets/flash -- only HIL can reach it). Parsing WITH the no-cover exclude gives the
    # unit-tested lines; the rest are the HIL-only lines. A unit-tested line needs no print;
    # the true coverage gap is a HIL-only line that ALSO no marker witnesses.
    pe = PythonParser(filename=filename, exclude=r"#\s*pragma:\s*no cover")
    pe.parse_source()
    unit = set(pe.statements)                             # covered by the host unit suite
    hil_only = stmts - unit                               # only HIL can cover these
    dom, _ = _dominators_for(filename)

    src = source.splitlines()
    owner = _func_owner(source)                            # line -> enclosing function name

    # Marker lines come from hil_coverage.find_source -- the SAME mapping the actual fold uses --
    # so the static set agrees with it. find_source matches the longest literal PREFIX of a marker,
    # so a format-string log ("boot: mounted %s") is recognized as its runtime marker
    # ("boot: mounted A"); a plain `substring in line` test would miss it and mis-report a gap.
    import hil_coverage
    rel = os.path.relpath(filename, _REPO)
    marker_lines = set()
    for sub in ota_cycle.COVERAGE:
        loc_file, loc_ln = hil_coverage.find_source(sub)
        if loc_file == rel and loc_ln:
            marker_lines.add(loc_ln)
    markers = [n for n in stmts if n in marker_lines]
    coverable = set()
    for m in markers:
        fn = owner.get(m)                                   # intraprocedural: a marker's real
        coverable |= {d for d in dom.get(m, ())             # dominators are all in its own
                      if d > 0 and owner.get(d) == fn}      # function -- clip to it (robust to
    coverable &= stmts                                      # coverage.py arc-graph quirks)
    # Restrict to RUNTIME PATHS -- statements inside a function body. Module-level scaffolding
    # (imports, def/class headers, constants) runs at load, not on a path, and isn't something
    # a UART print witnesses.
    body = {ln for ln in stmts if ln in owner}
    unit_body = unit & body                               # covered by the host unit suite
    hil_body = hil_only & body                            # HIL-only runtime lines
    hil_covered = coverable & hil_body                    # ...a marker's dominators witness these
    # A HIL-only line no marker witnesses is either a TRUE gap (must get a print) or an accepted
    # RESIDUAL a print inherently can't reach: a trailing return/raise (nothing runs after it), a
    # terminal machine.reset(), a generator's yield/EOF mechanics, a hot per-call closure the
    # milestone after it already proves, the logger proving itself, or an AE3-coproc / cloud path
    # with no HIL rig yet. Mark those `# hil-residual: <reason>` on the line (or
    # `# hil-residual-fn: <reason>` on the def, for a wholly-inherent function) -- exactly like the
    # RAM budget's `# ram-ok`. Everything else MUST be witnessed; a bare gap fails the CI audit.
    import re
    fn_residual = {}                                       # function name -> reason (def-level)
    for line in src:
        m = re.search(r"#\s*hil-residual-fn:\s*(.+?)\s*$", line)
        if m:
            d = re.match(r"\s*(?:async\s+)?def\s+(\w+)", line)
            if d:
                fn_residual[d.group(1)] = m.group(1)

    def _residual_reason(ln):
        line = src[ln - 1] if 1 <= ln <= len(src) else ""
        m = re.search(r"#\s*hil-residual:\s*(.+?)\s*$", line)
        return m.group(1) if m else fn_residual.get(owner.get(ln))

    raw_gaps = sorted(hil_body - coverable)
    residuals = {ln: _residual_reason(ln) for ln in raw_gaps if _residual_reason(ln)}
    gaps = [ln for ln in raw_gaps if ln not in residuals]  # unaccounted -> a print is required
    return {"file": os.path.relpath(filename, _REPO), "stmts": stmts, "body": body,
            "markers": sorted(markers), "coverable": coverable & body,
            "unit": unit_body, "hil_only": hil_body, "hil_covered": hil_covered,
            "gaps": gaps, "residuals": residuals, "owner": owner, "src": src}


def _func_owner(source):
    """{lineno: function_name} for every line inside a function body (the runtime paths)."""
    import ast
    owner = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.body[0].lineno
            for ln in range(node.body[0].lineno, end + 1):
                owner[ln] = node.name        # innermost wins (walk yields nested later)
    return owner


def _fmt_ranges(nums):
    """Collapse a sorted line list into compact ranges for readability."""
    nums = sorted(nums)
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append("%d" % nums[i] if i == j else "%d-%d" % (nums[i], nums[j]))
        i = j + 1
    return out


def main():
    files = sys.argv[1:] or DEFAULT_FILES
    t_unit = t_hil = t_hilcov = t_res = t_gap = 0
    for f in files:
        r = analyze(f)
        owner, src = r["owner"], r["src"]
        nu, nh, nhc = len(r["unit"]), len(r["hil_only"]), len(r["hil_covered"])
        nres, ng = len(r["residuals"]), len(r["gaps"])
        t_unit += nu
        t_hil += nh
        t_hilcov += nhc
        t_res += nres
        t_gap += ng
        print("\n=== %s ===" % r["file"])
        print("  unit-tested (host 100%% gate): %d | HIL-only (# pragma: no cover): %d "
              "-> witnessed: %d, residual: %d, GAP: %d" % (nu, nh, nhc, nres, ng))
        # The actionable list: HIL-only lines that are NEITHER witnessed NOR a marked residual --
        # every one must get a print (or a `# hil-residual: <reason>` if it inherently can't).
        by_func = {}
        for ln in r["gaps"]:
            by_func.setdefault(owner.get(ln), []).append(ln)
        for fn in sorted(by_func, key=lambda x: -len(by_func[x])):
            g = by_func[fn]
            print("    %s(): %d un-witnessed line(s) [%s]" % (fn, len(g), ", ".join(_fmt_ranges(g))))
            for ln in g[:3]:
                print("        %4d | %s" % (ln, src[ln - 1].strip()[:80]))
    denom = (t_hilcov + t_gap) or 1                        # coverable = witnessed + true gaps
    print("\n=== HIL coverage of coverable HW paths: %d/%d (%.0f%%) witnessed; %d residual; %d GAP ==="
          % (t_hilcov, t_hilcov + t_gap, 100.0 * t_hilcov / denom, t_res, t_gap))
    print("  (%d more lines are host-unit-tested, additive. %d lines are marked `# hil-residual` --"
          " inherently unreachable by a print, each with a reason.)" % (t_unit, t_res))
    if t_gap:
        print("  %d line(s) are UNACCOUNTED: witness them with a print, or mark `# hil-residual`."
              % t_gap)
    return 1 if t_gap else 0                               # non-zero exit == an unaccounted line


if __name__ == "__main__":
    sys.exit(main())
