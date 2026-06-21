"""Build a ROMFS image per project target: compile, convert, pack."""

from __future__ import annotations

import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from openmv_ota.project import load_project
from openmv_ota.project.errors import ProjectError
from openmv_ota.romfs.builder import build_image

from .compile import mpy
from .compile.models import MODEL_SUFFIXES, ModelContext, convert_model
from .errors import BuildError
from .staging import iter_files, stage_app


@dataclass
class BuildResult:
    target: str
    partition_index: int
    output: Path
    size: int
    capacity: int
    build_dir: Path | None = None  # set when --keep-build-dir


def build_romfs(
    project: str | Path,
    *,
    app: str | Path | None = None,
    output: str | Path | None = None,
    boards: list[str] | None = None,
    partition: int | None = None,
    compile_py: bool = True,
    convert_models: bool = True,
    mpy_extra: list[str] | None = None,
    vela_extra: list[str] | None = None,
    stedgeai_extra: list[str] | None = None,
    vela_optimise: str = "Performance",
    stedgeai_optimization: int = 3,
    firmware: str | Path | None = None,
    allow_oversize: bool = False,
    keep_build_dir: bool = False,
) -> list[BuildResult]:
    project = Path(project)
    try:
        p = load_project(project, firmware=firmware)  # verify=True: refuses on drift
    except ProjectError as e:
        raise BuildError(str(e), exit_code=e.exit_code) from None

    app_dir = Path(app) if app else project / "app"
    out_dir = Path(output) if output else project / "build"

    targets = _select_targets(p.targets, boards, partition)
    if not targets:
        raise BuildError("no matching targets in this project")

    if compile_py and p.mpy_cross_path is None:
        raise BuildError(
            "mpy-cross is not built; build the firmware first, or pass --no-compile-py"
        )

    ctx = ModelContext(
        sdk_home=p.sdk_home, vela_path=p.vela_path, stedgeai_path=p.stedgeai_path,
        vela_optimise=vela_optimise, stedgeai_optimization=stedgeai_optimization,
        vela_extra=list(vela_extra or []), stedgeai_extra=list(stedgeai_extra or []),
    )

    multi = {name for name, c in Counter(t.name for t in targets).items() if c > 1}
    out_dir.mkdir(parents=True, exist_ok=True)
    return [
        _build_one(p, t, app_dir, out_dir, ctx, multi,
                   compile_py=compile_py, convert_models=convert_models,
                   mpy_extra=list(mpy_extra or []), allow_oversize=allow_oversize,
                   keep_build_dir=keep_build_dir)
        for t in targets
    ]


def _select_targets(targets, boards, partition):
    sel = list(targets)
    if boards:
        sel = [t for t in sel if t.name in boards]
    if partition is not None:
        sel = [t for t in sel if t.partition_index == partition]
    return sel


def _build_one(p, t, app_dir, out_dir, ctx, multi, *, compile_py, convert_models,
               mpy_extra, allow_oversize, keep_build_dir) -> BuildResult:
    tmp = Path(tempfile.mkdtemp(prefix="openmv-ota-build-"))
    try:
        stage = stage_app(app_dir, tmp / "app")

        if compile_py:
            for src in iter_files(stage, (".py",)):
                mpy.compile_py(p.mpy_cross_path, t.mpy_args + mpy_extra, src,
                               src.with_suffix(".mpy"))
                src.unlink()

        if convert_models and t.npu:
            for model in iter_files(stage, MODEL_SUFFIXES):
                data = convert_model(t, ctx, model)
                if data is not None:
                    model.write_bytes(data)

        image = build_image(str(stage), t.alignment_rules)
        if len(image) > t.partition_size and not allow_oversize:
            raise BuildError(
                "%s image is %d bytes but the ROMFS partition holds %d (%d over); "
                "pass --allow-oversize"
                % (t.name, len(image), t.partition_size, len(image) - t.partition_size),
                exit_code=1,
            )

        name = "%s-p%d" % (t.name, t.partition_index) if t.name in multi else t.name
        out_path = out_dir / (name + ".romfs")
        out_path.write_bytes(image)
        return BuildResult(t.name, t.partition_index, out_path, len(image), t.partition_size,
                           build_dir=tmp if keep_build_dir else None)
    finally:
        if not keep_build_dir:
            shutil.rmtree(tmp, ignore_errors=True)
