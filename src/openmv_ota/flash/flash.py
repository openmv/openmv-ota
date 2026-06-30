"""Orchestrate flashing a board's built artifacts to their partitions.

``flash firmware`` writes the firmware image; ``flash romfs`` the app image; ``flash
factory`` the manufacturing program (firmware + the dual-slot factory image). Each resolves
every artifact + alt *before* writing anything (fail fast -- never flash firmware then
discover the romfs is missing), and leaves DFU only after the final write so a multi-step
flash keeps the device in the bootloader between steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openmv_ota.project import history

from . import dfu, runner, tools
from .errors import FlashError
from .targets import FlashConfig, flash_config


@dataclass(frozen=True)
class FlashStep:
    artifact: str       # logical target (firmware / romfs / coprocessor)
    file: Path
    alt: int
    argv: list[str]


def _output_dir(project: str, output: str | None) -> Path:
    return Path(output) if output else Path(project) / "build"


def _resolve_tool(dfu_util: str | None, sdk_home: Path | None, dry_run: bool) -> str:
    try:
        return tools.find_dfu_util(dfu_util, sdk_home)
    except FlashError:
        if not dry_run:                              # dry-run can show the command even
            raise                                    # when dfu-util isn't installed
        return dfu_util or "dfu-util"


def _run_steps(cfg: FlashConfig, spec: list[tuple[str, str]], tool: str, out_dir: Path,
               leave: bool, dry_run: bool) -> list[FlashStep]:
    resolved = []
    for artifact, fname in spec:
        f = out_dir / fname
        if not f.exists():
            raise FlashError("missing artifact %s -- build it first" % f)
        resolved.append((artifact, f, cfg.alt_of(artifact)))
    steps: list[FlashStep] = []
    last = len(resolved) - 1
    for i, (artifact, f, alt) in enumerate(resolved):
        argv = dfu.download_argv(tool, cfg.usb, alt, f, leave=leave and i == last)
        if not dry_run:
            runner.run(argv)
        steps.append(FlashStep(artifact, f, alt, argv))
    return steps


def _flash(project: str, board: str, spec: list[tuple[str, str]], action: str, *,
           output: str | None, dfu_util: str | None, sdk_home: Path | None,
           leave: bool, dry_run: bool) -> list[FlashStep]:
    cfg = flash_config(board)
    tool = _resolve_tool(dfu_util, sdk_home, dry_run)
    steps = _run_steps(cfg, spec, tool, _output_dir(project, output), leave, dry_run)
    if not dry_run:
        history.record(project, action, board=board,
                       files=[{"file": s.file.name, "alt": s.alt} for s in steps])
    return steps


def flash_firmware(project: str = ".", *, board: str, output: str | None = None,
                   dfu_util: str | None = None, sdk_home: Path | None = None,
                   coprocessor: bool = False, leave: bool = True, dry_run: bool = False
                   ) -> list[FlashStep]:
    spec = [("firmware", "%s-firmware.bin" % board)]
    if coprocessor:
        spec.append(("coprocessor", "%s-firmware-M55_HE.bin" % board))
    return _flash(project, board, spec, "flash-firmware", output=output, dfu_util=dfu_util,
                  sdk_home=sdk_home, leave=leave, dry_run=dry_run)


def flash_romfs(project: str = ".", *, board: str, output: str | None = None,
                dfu_util: str | None = None, sdk_home: Path | None = None,
                leave: bool = True, dry_run: bool = False) -> list[FlashStep]:
    spec = [("romfs", "%s-romfs.img" % board)]
    return _flash(project, board, spec, "flash-romfs", output=output, dfu_util=dfu_util,
                  sdk_home=sdk_home, leave=leave, dry_run=dry_run)


def flash_factory(project: str = ".", *, board: str, output: str | None = None,
                  dfu_util: str | None = None, sdk_home: Path | None = None,
                  coprocessor: bool = False, leave: bool = True, dry_run: bool = False
                  ) -> list[FlashStep]:
    spec = [("firmware", "%s-firmware.bin" % board)]
    if coprocessor:
        spec.append(("coprocessor", "%s-firmware-M55_HE.bin" % board))
    spec.append(("romfs", "%s-factory-romfs.img" % board))
    return _flash(project, board, spec, "flash-factory", output=output, dfu_util=dfu_util,
                  sdk_home=sdk_home, leave=leave, dry_run=dry_run)
