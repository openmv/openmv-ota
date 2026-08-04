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


def _erase_loop_body(fn):
    """The statements of the erase loop, in order, as (kind, text)."""
    for node in ast.walk(fn):
        if isinstance(node, (ast.While, ast.For)):
            return [(type(s).__name__, ast.unparse(s)) for s in node.body]
    return []


@pytest.mark.parametrize("index", [0, 1])
def test_the_feed_comes_BEFORE_the_erase_call(index):
    """On mimxrt a flash erase runs with interrupts disabled (flash.c __disable_irq), so SysTick
    cannot fire, PendSV cannot run, and machine.Timer -- a SOFT timer there -- cannot dispatch.
    Nothing can feed the watchdog until the erase returns. A feed placed AFTER the erase therefore
    hands each call whatever was left of the window, and hands the FIRST call whatever survived the
    manifest parse and TLS. That is what reset the RT1060 mid-erase, leaving FRONT half-erased and
    falling back to golden."""
    body = _erase_loop_body(_erase_funcs()[index])
    assert body, "erase loop not found"
    feeds = [i for i, (_, src) in enumerate(body) if src.strip() == "feed()"]
    erases = [i for i, (_, src) in enumerate(body) if "ioctl(" in src]
    assert feeds, "the erase loop must feed the watchdog"
    assert erases, "the erase loop must erase"
    assert min(feeds) < min(erases), (
        "feed() must PRECEDE the erase call -- nothing can feed while it runs.\n  loop body: %s"
        % [src for _, src in body])


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
