"""Tests for the top-level ``openmv-ota`` CLI dispatch."""

from __future__ import annotations

from openmv_ota import __version__
from openmv_ota.cli import build_parser, main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_no_command_prints_help(capsys):
    assert main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_unimplemented_init(capsys):
    # `init` is registered but not implemented yet.
    assert main(["init"]) == 2
    assert "not implemented" in capsys.readouterr().err.lower()


def test_build_parser_is_constructable():
    parser = build_parser()
    args = parser.parse_args(["romfs", "boards"])
    assert args._command == "romfs boards"
