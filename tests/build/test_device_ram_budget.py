"""The RAM budget guard: device code may not allocate by an amount it doesn't control.

Device modules run inside the *user's* app, so our memory is their memory. The
rules below are the ones we've actually been bitten by -- an unbounded spool
read, a ``read(-1)`` body, a wire-declared length handed straight to a reader.
They are cheap to state and cheap to check, so check them instead of trusting
review: this is what stops the pattern coming back in NEW code.

A legitimate exception is fine -- add ``# ram-ok: <reason>`` on the line and it
is skipped, so the rule stays honest instead of being deleted the first time it
is inconvenient.
"""

from __future__ import annotations

import io
import pathlib
import re
import tokenize

import pytest

DEVICE = pathlib.Path(__file__).resolve().parents[2] / \
    "src" / "openmv_ota" / "build" / "device"

# (regex, what's wrong). Each fires per-line unless the line is marked ram-ok.
BANNED = [
    (re.compile(r"\.read\(\s*-1\s*\)"),
     "read(-1) reads a whole body into RAM; cap it (see _read_capped)"),
    (re.compile(r"\.read\(\s*\)"),
     "read() with no size reads a whole file/body; pass a bounded size"),
    (re.compile(r"\bread_all\b"),
     "read_all() loads a whole file; stream it in bounded windows"),
    (re.compile(r"\breadall\b"),
     "readall() loads a whole stream; stream it in bounded windows"),
]


def _device_sources():
    return sorted(p for p in DEVICE.rglob("*.py"))


def test_the_device_tree_is_actually_being_scanned():
    # A guard on the guard: if the tree moves, fail loudly rather than silently
    # passing over zero files.
    srcs = _device_sources()
    assert len(srcs) >= 8, "device sources not found at %s" % DEVICE
    assert any(p.name == "csi.py" for p in srcs)


@pytest.mark.parametrize("path", _device_sources(), ids=lambda p: p.name)
def test_no_unbounded_reads_in_device_code(path):
    problems = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if "ram-ok:" in line or line.lstrip().startswith("#"):
            continue
        for pattern, why in BANNED:
            if pattern.search(line):
                problems.append("%s:%d: %s\n    %s" % (path.name, n, why, line.strip()))
    assert not problems, "RAM budget violations:\n" + "\n".join(problems)


@pytest.mark.parametrize("path", _device_sources(), ids=lambda p: p.name)
def test_every_device_module_states_the_ram_budget(path):
    # The rule lives with the code, so someone editing this file sees it without
    # having to find CLAUDE.md first.
    assert "RAM BUDGET:" in path.read_text(), (
        "%s is device code and must carry the RAM BUDGET note in its module "
        "docstring" % path.name)


# A read sized by the CEILING is the same bug as an unbounded read, and it is harder to see
# because it *looks* bounded. MicroPython's `f.read(n)` pre-allocates n bytes up front, so
# `f.read(_ASSET_MAX + 1)` demanded a 256 KiB contiguous block to read a 68 KiB installer.
# Measured on a Nicla Vision idling at 350 KiB free: `allocating 262145 bytes` failed, the
# exact-size read of the same file returned all 69591 bytes. It raised inside install(), and
# the poll loop swallowed it -- so that board took the offer and never installed, forever.
# Size reads by the FILE (os.stat) and keep the ceiling as a gate on the stat.
# The leading `_?` matters: our constants are `_ASSET_MAX`/`_CA_MAX`, and `\b[A-Z]` never
# matches inside `_CA_MAX` -- `_` is a word character, so there is no boundary before the C.
_CEILING_READ = re.compile(
    r"\.read\(\s*[^)]*?(\b_{0,2}[A-Z][A-Z0-9_]*(?:MAX|LIMIT|CEILING)\b|\blimit\b)")
_BIG_LITERAL = re.compile(r"\.read\(\s*(\d[\d_]*)\s*\*?\s*(\d[\d_]*)?")


def _too_big(match):
    a = int(match.group(1).replace("_", ""))
    b = int(match.group(2).replace("_", "")) if match.group(2) else 1
    return a * b >= 64 * 1024


def _code_lines(path):
    """Source lines with comments and string literals blanked out (line numbers preserved), so a
    rule matches CODE and not prose. The first cut of the rule below flagged the very docstring
    that DESCRIBES the bug -- a scan that reads its own explanation as a violation is worse than
    no scan, because the fix is to water down the wording."""
    src = path.read_text()
    out = src.splitlines()
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError:                      # pragma: no cover  (device sources all parse)
        return out
    for tok in toks:
        if tok.type not in (tokenize.STRING, tokenize.COMMENT):
            continue
        (r1, c1), (r2, c2) = tok.start, tok.end
        for r in range(r1, r2 + 1):
            line, a = out[r - 1], (c1 if r == r1 else 0)
            b = c2 if r == r2 else len(line)
            out[r - 1] = line[:a] + " " * (b - a) + line[b:]
    return out


@pytest.mark.parametrize("path", _device_sources(), ids=lambda p: p.name)
def test_no_reads_sized_by_a_ceiling_in_device_code(path):
    problems = []
    for n, line in enumerate(_code_lines(path), 1):
        if "ram-ok:" in line or line.lstrip().startswith("#"):
            continue
        hit = _CEILING_READ.search(line)
        big = _BIG_LITERAL.search(line)
        if hit or (big and _too_big(big)):
            problems.append(
                "%s:%d: read sized by a ceiling -- MicroPython pre-allocates that many bytes.\n"
                "    Size the read by the file (os.stat) and gate on the ceiling instead.\n"
                "    %s" % (path.name, n, line.strip()))
    assert not problems, "RAM budget violations:\n" + "\n".join(problems)


def test_the_ceiling_read_rule_catches_the_shipped_bug(tmp_path):
    """The exact lines that shipped, so the rule is proven against the real defect."""
    for line in ("        data = f.read(limit + 1)",
                 "                _ca_pem = f.read(_CA_MAX)",
                 "    body = sock.read(262145)"):
        hit = _CEILING_READ.search(line)
        big = _BIG_LITERAL.search(line)
        assert hit or (big and _too_big(big)), "rule missed: %s" % line
    for ok in ("        data = f.read(size + 1)",
               "            chunk = f.read(_CHUNK)",
               "    head = f.read(8)"):
        hit = _CEILING_READ.search(ok)
        big = _BIG_LITERAL.search(ok)
        assert not (hit or (big and _too_big(big))), "false positive: %s" % ok
