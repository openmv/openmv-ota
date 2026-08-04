"""Every flash-erase loop runs inside `relax()` -- the ISR feed.

Feeding BETWEEN erase calls cannot save you: the watchdog bite lands INSIDE one call. Measured on
the RT1060 HIL `watchdog` scenario -- the 4 MiB erase takes 54 s over ~1024 blocks (~52 ms each,
half the 100 ms window), and the first call after boot stalls for seconds. The device reset there
on every run, before even the first block witness, and fell back to golden: a watchdog-armed board
could not complete an OTA.

Checked on the AST, not the text: the previous source-scan in this suite matched its own
explanation, and a comment saying "runs under relax()" must never be what makes this pass.
"""

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[2] / \
    "src" / "openmv_ota" / "build" / "device" / "openmv_ota" / "data" / "installer.py"


def _erase_funcs():
    tree = ast.parse(_SRC.read_text())
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "erase"]


def _relaxed_loops(fn):
    """(loops, loops_inside_a_`with relax()`) for one erase function."""
    inside, loops = [], []

    def walk(node, relaxed):
        if isinstance(node, ast.With):
            called = any(isinstance(i.context_expr, ast.Call)
                         and getattr(i.context_expr.func, "id", None) == "relax"
                         for i in node.items)
            relaxed = relaxed or called
        if isinstance(node, (ast.While, ast.For)):
            loops.append(node)
            if relaxed:
                inside.append(node)
        for child in ast.iter_child_nodes(node):
            walk(child, relaxed)

    walk(fn, False)
    return loops, inside


def test_both_write_paths_define_an_erase():
    """XIP and block-device. If one disappears, this suite must not quietly cover one path."""
    assert len(_erase_funcs()) == 2


@pytest.mark.parametrize("index", [0, 1])
def test_every_erase_loop_is_inside_relax(index):
    fn = _erase_funcs()[index]
    loops, relaxed = _relaxed_loops(fn)
    assert loops, "erase() at line %d has no loop -- did the erase stop being incremental?" % fn.lineno
    assert len(relaxed) == len(loops), (
        "erase() at line %d has %d loop(s) but only %d inside `with relax()`. A feed BETWEEN "
        "erase calls cannot cover a bite INSIDE one." % (fn.lineno, len(loops), len(relaxed)))
