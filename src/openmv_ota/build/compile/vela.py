"""Convert a TensorFlow Lite model with Ethos-U Vela, the IDE's way.

IDE command (``vela.cpp``):
``vela <npu args> --optimise <Performance|Size> --config <vela.ini>
--verbose-performance --verbose-cycle-estimate --output-dir <tmp> <model>``
→ output ``<base>_vela.tflite`` (or ``_vela.lite``).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from ..errors import BuildError

# A model already converted for the Ethos-U contains this marker.
ALREADY_CONVERTED = b"ethos-u"


def convert(
    vela: str,
    npu_args: list[str],
    optimise: str,
    extra_args: list[str],
    vela_ini: bytes,
    model: Path,
) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        ini_path = tmpdir / "vela.ini"
        ini_path.write_bytes(vela_ini)
        cmd = [
            vela, *npu_args, "--optimise", optimise, *extra_args,
            "--config", str(ini_path),
            "--verbose-performance", "--verbose-cycle-estimate",
            "--output-dir", str(tmpdir), str(model),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            raise BuildError("vela not found: %s" % vela, exit_code=1) from None
        if proc.returncode != 0:
            raise BuildError(
                "vela failed on %s: %s" % (model.name, proc.stderr.strip()), exit_code=1
            )
        stem = model.stem
        for out in (tmpdir / (stem + "_vela.tflite"), tmpdir / (stem + "_vela.lite")):
            if out.exists():
                return out.read_bytes()
        raise BuildError("vela produced no output for %s" % model.name, exit_code=1)
