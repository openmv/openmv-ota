"""The leg matrix lives as PYTHON inside the workflow's heredoc -- so it must be executed, not just
parsed as YAML.

`yaml.safe_load` happily accepts a workflow whose embedded script is broken: it sees an opaque
string. That is not hypothetical -- `"advisory": true` (JSON spelling) shipped and failed the run
with `NameError: name 'true' is not defined`, the matrix never expanded, and every leg was skipped
while the workflow itself looked perfectly valid.

These tests run the snippet the way Actions does.
"""

import contextlib
import io
import json
import os
import pathlib
import textwrap
import unittest.mock

_WF = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "hil-ota.yml"


def _matrix(board, event):
    body = _WF.read_text().split("python3 - <<'PY'", 1)[1]
    body = body.split("\n", 1)[1]                      # drop the rest of the shell line
    snippet = textwrap.dedent(body.split("\n          PY", 1)[0])
    # patch the REAL environ: the snippet does its own `import os`, so a fake module handed in via
    # globals is silently rebound and every leg comes back regardless of BOARD.
    buf = io.StringIO()
    with unittest.mock.patch.dict(os.environ, {"BOARD": board, "EVENT": event}):
        with contextlib.redirect_stdout(buf):
            exec(snippet, {})
    return json.loads(buf.getvalue().strip().split("matrix=", 1)[1])["include"]


def test_the_embedded_matrix_script_actually_runs():
    """A NameError here skips every leg while the workflow still parses as valid YAML."""
    legs = _matrix("all", "pull_request")
    assert legs, "the fleet matrix must not be empty"
    assert all({"board", "network", "label"} <= set(leg) for leg in legs)


def test_a_dispatch_selects_just_that_board():
    legs = _matrix("ARDUINO_NICLA_VISION", "workflow_dispatch")
    assert [leg["board"] for leg in legs] == ["ARDUINO_NICLA_VISION"]


def test_advisory_is_a_python_bool_not_the_json_spelling():
    """`true` is JSON; this heredoc is Python. json.dumps still emits `true` for the matrix, so the
    workflow's `matrix.advisory == true` expression keeps working."""
    legs = _matrix("all", "pull_request")
    advisory = [leg for leg in legs if "advisory" in leg]
    assert advisory, "expected at least one advisory leg"
    for leg in advisory:
        assert leg["advisory"] is True
    assert '"advisory": true' in json.dumps({"include": advisory})
