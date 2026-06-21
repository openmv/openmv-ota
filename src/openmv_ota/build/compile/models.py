"""Dispatch a model to the right NPU compiler, with already-converted detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..data import read_firmware_file
from ..errors import BuildError
from . import stedgeai, vela

# Model file extensions the NPU compilers accept.
MODEL_SUFFIXES = (".tflite", ".lite", ".onnx")


@dataclass
class ModelContext:
    sdk_home: Path
    vela_path: str | None = None
    stedgeai_path: str | None = None
    vela_optimise: str = "Performance"
    stedgeai_optimization: int = 3
    vela_extra: list[str] = field(default_factory=list)
    stedgeai_extra: list[str] = field(default_factory=list)


def convert_model(target, ctx: ModelContext, model: Path) -> bytes | None:
    """Convert ``model`` for ``target``'s NPU. Returns the converted bytes, or
    ``None`` when the model is already converted (and should be packed as-is)."""
    data = model.read_bytes()
    npu = target.npu
    cfg = target.npu_config or {}
    args = list(cfg.get("args", []))

    if npu == "vela":
        if vela.ALREADY_CONVERTED in data:
            return None
        if not ctx.vela_path:
            raise BuildError("this SDK has no vela; cannot convert %s" % model.name, exit_code=1)
        ini = read_firmware_file(cfg["iniFilePath"])
        return vela.convert(ctx.vela_path, args, ctx.vela_optimise, ctx.vela_extra, ini, model)

    if npu == "stedgeai":
        if data.startswith(stedgeai.ALREADY_CONVERTED):
            return None
        if not ctx.stedgeai_path:
            raise BuildError("this SDK has no stedgeai; cannot convert %s" % model.name, exit_code=1)
        neuralart = read_firmware_file(cfg["jsonFilePath"])
        mpool = read_firmware_file(cfg["mpoolFilePath"])
        return stedgeai.convert(
            ctx.stedgeai_path, ctx.sdk_home, args, ctx.stedgeai_extra,
            neuralart, mpool, ctx.stedgeai_optimization, model,
        )

    raise BuildError("unsupported NPU type %r for %s" % (npu, target.name), exit_code=1)
