"""Dropping the polling stack from a board whose firmware has no TLS.

The OPENMV2 (F427) installs a ~31 KB image with ~650 bytes of heap to spare. Measured on
the bench: 352 bytes of extra runtime bytecode took it from 6/6 installs passing to ~1 in
3 FAILING -- and a failure there is not a retry, it is an erased romfs and a board with no
app until someone reflashes it. That board has no `ssl`, so the entire check-in/polling
stack can never execute on it; the pack cuts it rather than spend the margin on code the
board cannot run.

These pin the properties that make the cut safe: the region is self-contained, what is
left still compiles and defines what the rest of the module uses, and no capable board's
runtime is touched.
"""

import ast
import io
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME = _ROOT / "src/openmv_ota/build/device/openmv_ota/__init__.py"
sys.path.insert(0, str(_ROOT / "src"))

from openmv_ota.build.pystrip import drop_network_runtime  # noqa: E402
from openmv_ota.romfs import boards as boards_mod  # noqa: E402

_SRC = io.open(_RUNTIME, encoding="utf-8").read()


def test_only_the_board_without_tls_drops_it():
    """One board, on measurement, not taste. The M7 and H7 classics HAVE ssl (checked on
    the bench: present, 42 KB and 320 KB free) and keep the full runtime."""
    assert boards_mod.get_board("OPENMV2").ota_runtime_drops_network is True
    for other in ("OPENMV3", "OPENMV4", "OPENMV4P", "OPENMV_N6", "OPENMV_RT1060", "OPENMV_AE3"):
        assert boards_mod.get_board(other).ota_runtime_drops_network is False, other


def test_the_region_is_self_contained():
    """Nothing outside the cut may use a name defined inside it, or the trimmed runtime
    would import and then fail at the first call."""
    tree = ast.parse(_SRC)
    lines = _SRC.splitlines()
    lo = next(i for i, ln in enumerate(lines, 1) if ln.startswith("# --- NETWORK RUNTIME: begin"))
    hi = next(i for i, ln in enumerate(lines, 1) if ln.startswith("# --- NETWORK RUNTIME: end"))
    inside = {n.name for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and lo <= n.lineno <= hi}
    assert inside, "the markers matched no definitions -- did the region move?"
    # `run` is re-provided as a stub, so a reference to it is fine; nothing else may leak.
    leaked = sorted({n.id for n in ast.walk(tree)
                     if isinstance(n, ast.Name) and n.id in (inside - {"run"})
                     and not (lo <= getattr(n, "lineno", 0) <= hi)})
    assert leaked == [], "used outside the droppable region: %s" % leaked


def test_what_is_left_compiles_and_keeps_the_public_surface():
    out = drop_network_runtime(_SRC)
    compile(out, "<trimmed>", "exec")
    tree = ast.parse(out)
    names = {n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for kept in ("install", "status", "slots", "confirm", "identity", "sync", "run"):
        assert kept in names, "%s must survive the cut" % kept
    assert "_checkin" not in names and "_poll_forever" not in names
    assert len(out) < len(_SRC) - 20000, "the cut should remove tens of KB, not a few lines"


def test_run_is_left_saying_why_rather_than_missing():
    """A board that cannot poll should fail with a REASON, not AttributeError -- someone
    calling run() there deserves to be told, not left guessing at a missing name."""
    import asyncio

    from openmv_ota.build import pystrip
    ns = {}
    exec(compile(pystrip._NET_STUB, "<stub>", "exec"), ns)
    with pytest.raises(OSError) as e:
        asyncio.run(ns["run"]("https://example"))
    assert "no TLS" in str(e.value) and "install(" in str(e.value)


def test_the_markers_must_be_exactly_one_pair():
    with pytest.raises(ValueError):
        drop_network_runtime("x = 1\n")
    with pytest.raises(ValueError):
        drop_network_runtime("# --- NETWORK RUNTIME: end\n# --- NETWORK RUNTIME: begin\n")


def test_the_pack_cuts_it_for_that_board_and_only_that_board(tmp_path):
    """The build hook itself, not just the cutter: stage a runtime lib the way a real
    pack does and run the inject for a flagged board and an unflagged one."""
    from openmv_ota.build.romfs import _runtime_inject

    def stage_for(board):
        stage = tmp_path / board
        lib = stage / "lib" / "openmv_ota"
        lib.mkdir(parents=True)
        (lib / "__init__.py").write_text(_SRC, encoding="utf-8")
        (lib / "data").mkdir()
        _runtime_inject(tmp_path, board, [])(stage)
        return (lib / "__init__.py").read_text(encoding="utf-8")

    dropped = stage_for("OPENMV2")
    assert "def _checkin(" not in dropped and "async def _poll_forever(" not in dropped
    assert "no TLS" in dropped
    assert len(dropped) < len(_SRC) - 20000

    kept = stage_for("OPENMV3")
    assert kept == _SRC, "a board with ssl must get the runtime byte-for-byte"
