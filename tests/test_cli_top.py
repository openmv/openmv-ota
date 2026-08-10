"""Tests for the top-level ``openmv-ota`` CLI dispatch."""

from __future__ import annotations

import pytest

from openmv_ota import __version__
from openmv_ota.cli import build_parser, main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_no_command_prints_help(capsys):
    assert main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_unknown_command_rejected(capsys):
    # The removed `init` stub (and any other unknown verb) is rejected by argparse.
    with pytest.raises(SystemExit):
        main(["init"])
    assert "invalid choice" in capsys.readouterr().err.lower()


def test_build_parser_is_constructable():
    parser = build_parser()
    args = parser.parse_args(["romfs", "boards"])
    assert args._command == "romfs boards"


def test_every_documented_verb_exists():
    """Docs are the only place a verb can be renamed without anything noticing.

    This caught two: README documented `build ota-image`, which had become `build ota-romfs`,
    and server.md documented `client devices --board-id`, which is `--product-id`. Both would
    have failed for a reader following the docs literally, and neither shows up in a test that
    only exercises the CLI.

    Verb PATHS only -- flags are checked against `--help` text below rather than executed,
    since running them needs a project."""
    import re
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]

    def verbs_of(rest):
        out = []
        for word in rest.split():
            if word.startswith("-") or "<" in word or "." in word or "/" in word:
                break
            out.append(word)
        return tuple(out[:3])

    # A command, not a mention: either it starts a line (a shell block) or it fills an inline
    # code span. Prose like "the openmv-ota repository" and "git -C openmv-ota remote add"
    # are not invocations, and treating them as verbs is how this check cries wolf.
    invocation = re.compile(r"(?m)^\s*(?:\$ )?openmv-ota ([a-z][a-z0-9 _-]*)")
    inline = re.compile(r"`openmv-ota ([a-z][a-z0-9 _-]*)`")
    paths = set()
    for doc in [*sorted((root / "docs").glob("*.md")), root / "README.md"]:
        text = doc.read_text()
        for m in [*invocation.finditer(text), *inline.finditer(text)]:
            v = verbs_of(m.group(1))
            if v:
                paths.add(v)

    broken = []
    for path in sorted(paths):
        r = subprocess.run([sys.executable, "-m", "openmv_ota.cli", *path, "--help"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            broken.append(" ".join(path))
    assert not broken, (
        "documented verb paths that do not exist: %r. Either the docs are stale or the verb "
        "was renamed without updating them." % broken)
