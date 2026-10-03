"""`client release publish` works on a plain `pip install openmv-ota` -- no `[server]` extra.

Getting started tells people to `pip install openmv-ota`, and a publish from such a venv failed
with "the client needs extra packages": the client's one HTTP dependency, httpx, lived in the
server extra. It is a base dependency now, and nothing on the client's path may import a
server-only package.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# The server extra's packages (and the backends' extras) -- absent on a base install.
_SERVER_ONLY = ("fastapi", "uvicorn", "pydantic_settings", "pydantic", "starlette", "multipart",
                "python_multipart", "boto3", "psycopg")

_PROBE = """
import importlib.abc, sys
class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in %r:
            raise ImportError("server-only package on the client path: " + name)
sys.meta_path.insert(0, _Block())
from openmv_ota.cli import main
from openmv_ota.client import api, cli                      # noqa: F401
from openmv_ota.ota import geometry, manifest, payload, trailer   # noqa: F401  (publish's lazy imports)
from openmv_ota.build import sbom                           # noqa: F401
from openmv_ota.project import passphrase, payload_keys, project  # noqa: F401
api.Api(type("Cfg", (), {"token": "t", "server_url": "https://ota.example"})())  # a real httpx client
try:
    main(["client", "release", "publish", "--help"])
except SystemExit as e:
    assert e.code == 0, e.code
print("BASE-OK")
"""


def test_httpx_is_a_base_dependency_not_a_server_extra():
    meta = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]
    assert any(d.startswith("httpx") for d in meta["dependencies"])
    assert not any(d.startswith("httpx") for d in meta["optional-dependencies"]["server"])


def test_the_publish_path_imports_nothing_from_the_server_extra():
    out = subprocess.run([sys.executable, "-c", _PROBE % (_SERVER_ONLY,)], capture_output=True,
                         text=True, env={"PYTHONPATH": str(_ROOT / "src"), "PATH": "/usr/bin:/bin"},
                         timeout=60)
    assert "BASE-OK" in out.stdout, out.stderr[-2000:]
