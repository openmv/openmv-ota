"""Convert a model with ST Edge AI (Neural-ART), the IDE's way.

IDE behaviour (``stedgeai.cpp``): copy the board's ``neuralart.json`` +
``stm32n6.mpool`` into a working dir, substitute the optimization level into the
``neuralart.json`` ``%`` placeholder, then run from that dir:
``stedgeai generate --model <model> --relocatable <npu args>`` → output at
``st_ai_output/network_rel.bin``.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from ..errors import BuildError

# A model already converted for the Neural-ART starts with this marker.
ALREADY_CONVERTED = b"NBIN"


def render_neuralart(template: bytes, optimization: int) -> bytes:
    """Substitute the optimization level into the ``%`` placeholder (IDE-style)."""
    return template.replace(b"%", b"--optimization %d" % optimization)


def convert(
    stedgeai: str,
    sdk_home: Path,
    npu_args: list[str],
    extra_args: list[str],
    neuralart: bytes,
    mpool: bytes,
    optimization: int,
    model: Path,
) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        (workdir / "neuralart.json").write_bytes(render_neuralart(neuralart, optimization))
        (workdir / "stm32n6.mpool").write_bytes(mpool)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["STEDGEAI_CORE_DIR"] = str(sdk_home / "stedgeai")
        prepend = os.pathsep.join([str(Path(stedgeai).parent), str(sdk_home / "gcc" / "bin")])
        env["PATH"] = prepend + os.pathsep + env.get("PATH", "")

        cmd = [stedgeai, "generate", "--model", str(model), "--relocatable",
               *npu_args, *extra_args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(workdir), env=env)
        except FileNotFoundError:
            raise BuildError("stedgeai not found: %s" % stedgeai, exit_code=1) from None
        if proc.returncode != 0:
            raise BuildError(
                "stedgeai failed on %s: %s" % (model.name, proc.stderr.strip()), exit_code=1
            )
        out = workdir / "st_ai_output" / "network_rel.bin"
        if not out.exists():
            raise BuildError("stedgeai produced no output for %s" % model.name, exit_code=1)
        return out.read_bytes()
