"""A device module's top-level calls only use names defined above them.

Device modules wire themselves into openmv_ota at import time with a top-level call
(``_register()``). On the host those calls never run (the openmv_ota seam is absent), so
a call placed above the functions it names passed every test and shipped: on the camera
the import raised NameError, main.py died before openmv_ota.run() started, and every
board sat unconfirmed in its trial slot, never checking in again. This walks each device
module's AST instead of running it."""

import ast
from pathlib import Path

import pytest

DEVICE = Path(__file__).resolve().parents[2] / "src" / "openmv_ota" / "build" / "device"
MODULES = sorted(p for p in DEVICE.rglob("*.py") if "data" not in p.parts)


def _defined_at(tree):
    """Module-level name -> the line where it is first bound."""
    out = {}
    for node in tree.body:
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        for n in names:
            out.setdefault(n, node.lineno)
    return out


def _late_names(tree):
    """(call line, name) for each module-level name a top-level call reaches before it
    is defined -- directly in the call, or inside the function it calls."""
    defined = _defined_at(tree)
    late, funcs = [], {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node          # the definition in force at the next call
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
            continue
        reached = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        callee = node.value.func
        if isinstance(callee, ast.Name) and callee.id in funcs:
            reached |= {n.id for n in ast.walk(funcs[callee.id])
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        late += [(node.lineno, n) for n in sorted(reached)
                 if n in defined and defined[n] > node.lineno]
    return late


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(DEVICE)))
def test_top_level_calls_only_use_names_defined_above(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert _late_names(tree) == [], f"{path.name}: names used before they are defined"


def test_the_guard_catches_the_bug_it_was_written_for():
    tree = ast.parse("def _register():\n    hook(_relieve)\n\n_register()\n\n"
                     "def _relieve(level):\n    pass\n\n"
                     "def _register(stream):\n    pass\n")    # a later same-named def
    assert _late_names(tree) == [(4, "_relieve")]
