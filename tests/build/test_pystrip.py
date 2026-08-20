"""Tests for the pack-time source stripper (build.pystrip)."""

import pytest

from openmv_ota.build.pystrip import strip_python_source

SRC = '''\
"""Module docstring, multi
line."""
# a comment
import io  # trailing comment


def f(a):
    """Docstring."""
    # comment inside
    s = "# not a comment"
    d = """not a docstring: it is assigned"""
    return s + d, a


def only_doc():
    """A function whose whole body is its docstring."""


class C:
    """Class docstring."""
    x = 1
'''


def test_strips_comments_docstrings_blanks():
    out = strip_python_source(SRC)
    assert "# a comment" not in out and "# trailing comment" not in out
    assert "Module docstring" not in out and "Docstring." not in out
    assert '"# not a comment"' in out                    # strings that look like comments stay
    assert "not a docstring: it is assigned" in out      # assigned strings are not docstrings
    assert "\n\n" not in out                             # no blank lines
    ns = {}
    exec(compile(out, "s", "exec"), ns)                  # semantics survive
    assert ns["f"](2) == ("# not a commentnot a docstring: it is assigned", 2)
    ns["only_doc"]()                                     # docstring-only body still has a body
    assert ns["C"].x == 1


def test_shrinks_the_real_installer():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2]
           / "src/openmv_ota/build/device/openmv_ota/data/installer.py").read_text()
    out = strip_python_source(src)
    assert len(out) < len(src) * 0.5                     # the whole point
    compile(out, "installer.py", "exec")


def test_bad_source_raises():
    with pytest.raises(SyntaxError):
        strip_python_source("def broken(:\n")


def test_statement_leading_string_concat_is_not_a_docstring():
    # a STRING first on the line but CONTINUED (implicit concat) is an expression,
    # not a docstring -- it must survive whole
    out = strip_python_source('"""part one""" " and two"\nx = 1\n')
    assert "part one" in out and "and two" in out and "x = 1" in out


def test_docstring_with_trailing_comment_still_collapses():
    out = strip_python_source('def f():\n    """doc"""  # trailing\n    return 1\n')
    assert "doc" not in out and "trailing" not in out and "return 1" in out
