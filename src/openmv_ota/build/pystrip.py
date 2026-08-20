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
