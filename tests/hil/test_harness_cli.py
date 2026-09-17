"""The HIL harness calls the real CLI. This proves the calls still exist.

`client release publish --rollout __default__:100` went stale on 2026-08-31, when the
CLI regrouped that flag into `--cohort` / `--percent`. Nothing noticed for three weeks,
because the fleet only runs on a device PR and there was not one -- so the next device
PR paid for it with a 12-leg run that failed on its first publish, an hour in.

The guard is not a comment: every verb and flag the harness passes to `openmv-ota` is
checked here, on every push, against the parser it will actually meet.
"""

from __future__ import annotations

import argparse
import ast
import os

from openmv_ota.cli import build_parser

_HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "ci", "hil", "ota_cycle.py")


def _is_cli_argv(node):
    """A list literal whose first element is ``ota("openmv-ota")``."""
    if not isinstance(node, ast.List) or not node.elts:
        return False
    head = node.elts[0]
    return (isinstance(head, ast.Call) and isinstance(head.func, ast.Name)
            and head.func.id == "ota" and len(head.args) == 1
            and getattr(head.args[0], "value", None) == "openmv-ota")


def _invocations():
    """``[(verbs, flags)]`` for every argv the harness builds.

    Verbs are the leading string literals, flags are every literal starting with "-".
    Runtime values (the project dir, the board, a server URL) are skipped -- what rots
    is the SHAPE of a call, not the values poured into it. A call whose VERB is a
    variable (`flash <op>`) yields the verbs it does name plus ``partial=True``, and its
    flags are then checked against every subcommand that verb could reach.
    """
    tree = ast.parse(open(_HARNESS).read())
    out = []
    for node in ast.walk(tree):
        if not _is_cli_argv(node):
            continue
        verbs, flags, leading, partial = [], [], True, False
        for el in node.elts[1:]:
            value = el.value if isinstance(el, ast.Constant) else None
            if isinstance(value, str) and value.startswith("-"):
                flags.append(value)
                leading = False
            elif isinstance(value, str) and leading:
                verbs.append(value)
            else:
                partial = partial or leading    # a variable where a verb could go
                leading = False
        out.append((tuple(verbs), flags, partial))
    return out


def _subparser(verbs):
    """The parser the CLI reaches for ``verbs``, or None if that path is gone."""
    parser = build_parser()
    for verb in verbs:
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]  # noqa: SLF001
        choices = subs[0].choices if subs else {}
        if verb not in choices:
            return None
        parser = choices[verb]
    return parser


def _options(parser):
    return {opt for action in parser._actions for opt in action.option_strings}  # noqa: SLF001


def _known_flags(parser, partial):
    """The flags a call may pass: this parser's, plus -- when the subcommand itself is a
    runtime value -- those of every subcommand it could resolve to."""
    known = _options(parser)
    if partial:
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]  # noqa: SLF001
        for sub in subs:
            for child in sub.choices.values():
                known |= _options(child)
    return known


def test_the_harness_calls_verbs_that_exist():
    found = _invocations()
    assert found, "no openmv-ota invocations found in the harness -- did its shape change?"
    for verbs, _flags, _partial in found:
        assert _subparser(verbs) is not None, \
            "the harness runs `openmv-ota %s`, which the CLI no longer has" % " ".join(verbs)


def test_the_harness_passes_flags_that_exist():
    for verbs, flags, partial in _invocations():
        known = _known_flags(_subparser(verbs), partial)
        for flag in flags:
            assert flag in known, (
                "the harness passes `%s` to `openmv-ota %s`, which does not take it -- the "
                "fleet fails on its first use, an hour into a run"
                % (flag, " ".join(verbs)))
