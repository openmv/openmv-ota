"""Pack-time Python source stripping for files that must ship as SOURCE.

``data/installer.py`` is exec'd into RAM on-device (it erases the slot it runs
from), so unlike every other ``.py`` it cannot be compiled to ``.mpy`` -- and in
the repo it is ~70% comments and docstrings. Shipping those costs flash in every
image (fatal on a single-image classic, whose whole slot is ~112 KiB) and RAM at
install time (the whole source is read + compiled on the device). Stripping at
PACK time keeps the repo file fully documented while the device gets only code:
comments dropped, docstrings collapsed to ``''`` (a docstring may be load-bearing
as a statement, e.g. a function whose body is only a docstring), blank lines
removed. Semantics are otherwise identical; only line numbers shift.
"""

from __future__ import annotations

import io
import tokenize

_SKIP_AFTER = (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING)


def strip_python_source(src: str) -> str:
    """``src`` minus comments, docstrings (collapsed to ``''``), and blank lines.
    The result must still compile; callers verify (and we re-verify cheaply here)."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError as e:                 # unterminated construct etc.
        raise SyntaxError(str(e)) from None
    keep = []
    for i, t in enumerate(toks):
        if t.type == tokenize.COMMENT:
            continue
        if t.type == tokenize.STRING:
            j = i - 1
            while j >= 0 and toks[j].type in (tokenize.NL, tokenize.COMMENT):
                j -= 1
            if j < 0 or toks[j].type in _SKIP_AFTER:
                k = i + 1
                while k < len(toks) and toks[k].type in (tokenize.NL, tokenize.COMMENT):
                    k += 1
                if k < len(toks) and toks[k].type == tokenize.NEWLINE:
                    keep.append(tokenize.TokenInfo(
                        tokenize.STRING, "''", t.start, t.end, t.line))
                    continue
        keep.append(t)
    out = tokenize.untokenize(keep)
    lines = [line.rstrip() for line in out.splitlines()]
    text = "\n".join(line for line in lines if line.strip()) + "\n"
    compile(text, "<stripped>", "exec")      # never ship something that cannot parse
    return text


_NET_BEGIN = "# --- NETWORK RUNTIME: begin"
_NET_END = "# --- NETWORK RUNTIME: end"
_NET_STUB = (
    "async def run(*a, **k):\n"
    "    raise OSError('no TLS on this board -- install(path) from a file instead')\n"
)


def drop_network_runtime(src: str) -> str:
    """``src`` with the marked polling stack removed and ``run()`` left as a stub saying why.

    The region needs ``ssl``. A board whose firmware carries none can never execute it, so
    the bytecode is pure cost -- and on the OPENMV2 (F427) that cost is the whole margin:
    it installs a ~31 KB image with ~650 bytes of heap to spare, and losing that margin
    means the install erases the romfs and never writes it, leaving the board with no app.

    Cutting is textual and exact -- the markers are single lines the runtime carries -- so
    a capable board's module stays byte-identical to the repo's. The result is compiled
    here: never ship something that will not import.
    """
    lines = src.splitlines(True)
    starts = [i for i, ln in enumerate(lines) if ln.startswith(_NET_BEGIN)]
    ends = [i for i, ln in enumerate(lines) if ln.startswith(_NET_END)]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise ValueError("expected one NETWORK RUNTIME begin/end pair, in order; "
                         "found %d begin and %d end" % (len(starts), len(ends)))
    out = "".join(lines[:starts[0]]) + _NET_STUB + "".join(lines[ends[0] + 1:])
    compile(out, "<runtime>", "exec")
    return out
