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


def test_python_m_openmv_ota_takes_the_clis_imports_from_outside_the_working_directory(
        tmp_path):
    """`python -m openmv_ota` run inside a project that holds an `argparse.py`: the CLI's own
    imports come from the standard library, not from the project."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    import openmv_ota
    (tmp_path / "argparse.py").write_text("open('ran', 'w').close()\n")
    env = dict(os.environ, PYTHONPATH=str(Path(openmv_ota.__file__).parents[1]))
    r = subprocess.run([sys.executable, "-m", "openmv_ota", "--version"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == openmv_ota.__version__, r.stderr
    assert not (tmp_path / "ran").exists()


def test_python_dash_p_m_openmv_ota_runs_this_package_not_one_in_the_working_directory(
        tmp_path):
    """Python finds the package `-m` names before its `__main__` can drop the working
    directory, so a project's own `openmv_ota/` runs in place of the tool; with `-P`, the start
    the changelog gives for that, Python never looks there."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    import openmv_ota
    planted = tmp_path / "openmv_ota"
    planted.mkdir()
    (planted / "__init__.py").write_text("open('ran', 'w').close()\n")
    (planted / "__main__.py").write_text("")
    env = dict(os.environ, PYTHONPATH=str(Path(openmv_ota.__file__).parents[1]))
    r = subprocess.run([sys.executable, "-P", "-m", "openmv_ota", "--version"], cwd=tmp_path,
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == openmv_ota.__version__, r.stderr
    assert not (tmp_path / "ran").exists()


def test_main_module_drops_the_working_directory(tmp_path, monkeypatch):
    import runpy
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    monkeypatch.setattr(sys, "argv", ["openmv-ota", "--version"])
    with pytest.raises(SystemExit) as e:
        runpy.run_module("openmv_ota", run_name="__main__")
    assert e.value.code == 0 and str(tmp_path) not in sys.path


def test_the_working_directory_is_not_on_the_import_path(tmp_path, monkeypatch):
    """Started as `python -m openmv_ota`, the working directory was first on the import
    path: a `boto3.py` or `mpy_cross.py` in a project someone else made was the one
    imported. The helpers this runs with `-m` get `-P` for the same reason."""
    import sys

    from openmv_ota import cli
    monkeypatch.chdir(tmp_path)
    for first in ("", str(tmp_path)):
        monkeypatch.setattr(sys, "path", [first, "/usr/lib/python3"])
        cli._drop_cwd_from_path()
        assert sys.path == ["/usr/lib/python3"]
    monkeypatch.setattr(sys, "path", ["/usr/bin", "/usr/lib/python3"])       # a console script
    cli._drop_cwd_from_path()
    assert sys.path == ["/usr/bin", "/usr/lib/python3"]
    # ...run from the directory this package itself is in, it stays
    import os
    src = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    monkeypatch.chdir(src)
    monkeypatch.setattr(sys, "path", [src, "/usr/lib/python3"])
    cli._drop_cwd_from_path()
    assert sys.path[0] == src


def test_a_filesystem_refusal_is_one_line_not_a_traceback(monkeypatch, capsys):
    """`build` a file where a directory is made, a directory where a file is written: the
    command's OSError prints as an error (exit 1), and a closed pipe ends it quietly."""
    import argparse

    from openmv_ota import cli

    def fails(err):
        def run(args):
            raise err
        return run

    for err, said in ((NotADirectoryError(20, "Not a directory", "build/x"), "Not a directory"),
                      (IsADirectoryError(21, "Is a directory", "device/openmv_log.py"),
                       "Is a directory")):
        monkeypatch.setattr(cli.argparse.ArgumentParser, "parse_args",
                            lambda self, argv=None, e=err: argparse.Namespace(func=fails(e)))
        assert cli.main(["x"]) == 1
        err = capsys.readouterr().err
        assert err.startswith("error: [Errno") and said in err and err.count("\n") == 1
    monkeypatch.setattr(cli.argparse.ArgumentParser, "parse_args",
                        lambda self, argv=None: argparse.Namespace(func=fails(BrokenPipeError())))
    assert cli.main(["x"]) == 1 and capsys.readouterr().err == ""
