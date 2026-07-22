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

import pathlib
import re

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
