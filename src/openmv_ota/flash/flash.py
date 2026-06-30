"""Orchestrate flashing a board's built artifacts to their partitions.

``flash firmware`` writes the firmware image; ``flash romfs`` the app image; ``flash
factory`` the manufacturing program (firmware + the dual-slot factory image). Each resolves
every artifact + alt *before* writing anything (fail fast -- never flash firmware then
discover the romfs is missing), and resets the board only after the final write so a
multi-step flash keeps the device in the bootloader between steps.

A step is ``(logical-artifact, default-filename-suffix)``; the filename is ``<board>-<suffix>``
unless the board's ``flash.file`` map overrides it (the AE3's per-core ``firmware-M55_HP.bin``).
On the AE3 (the one multi-core board) ``--coprocessor`` adds the HE-core firmware, and for a
factory flash its romfs too.
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
    artifact: str       # logical target (firmware / romfs / coprocessor / coprocessor_romfs)
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


def _run_steps(cfg: FlashConfig, board: str, spec: list[tuple[str, str]], tool: str,
               out_dir: Path, reset: bool, dry_run: bool) -> list[FlashStep]:
    resolved = []
    for artifact, suffix in spec:
        alt = cfg.alt_of(artifact)                   # the clearest error (unsupported) first
        f = out_dir / ("%s-%s" % (board, cfg.filename(artifact, suffix)))
        if not f.exists():
            raise FlashError("missing artifact %s -- build it first" % f)
        resolved.append((artifact, f, alt))
    steps: list[FlashStep] = []
    last = len(resolved) - 1
    for i, (artifact, f, alt) in enumerate(resolved):
        argv = dfu.download_argv(tool, cfg.usb, alt, f, reset=reset and i == last)
        if not dry_run:
            runner.run(argv)
        steps.append(FlashStep(artifact, f, alt, argv))
    return steps


def _flash(project: str, board: str, spec: list[tuple[str, str]], action: str, *,
           output: str | None, dfu_util: str | None, sdk_home: Path | None,
           reset: bool, dry_run: bool) -> list[FlashStep]:
    cfg = flash_config(board)
    tool = _resolve_tool(dfu_util, sdk_home, dry_run)
    steps = _run_steps(cfg, board, spec, tool, _output_dir(project, output), reset, dry_run)
    if not dry_run:
        history.record(project, action, board=board,
                       files=[{"file": s.file.name, "alt": s.alt} for s in steps])
    return steps


def flash_firmware(project: str = ".", *, board: str, output: str | None = None,
                   dfu_util: str | None = None, sdk_home: Path | None = None,
                   coprocessor: bool = False, reset: bool = True, dry_run: bool = False
                   ) -> list[FlashStep]:
    spec = [("firmware", "firmware.bin")]
    if coprocessor:
        spec.append(("coprocessor", "firmware-M55_HE.bin"))
    return _flash(project, board, spec, "flash-firmware", output=output, dfu_util=dfu_util,
                  sdk_home=sdk_home, reset=reset, dry_run=dry_run)


def flash_romfs(project: str = ".", *, board: str, output: str | None = None,
                dfu_util: str | None = None, sdk_home: Path | None = None,
                reset: bool = True, dry_run: bool = False) -> list[FlashStep]:
    spec = [("romfs", "romfs.img")]
    return _flash(project, board, spec, "flash-romfs", output=output, dfu_util=dfu_util,
                  sdk_home=sdk_home, reset=reset, dry_run=dry_run)


def flash_factory(project: str = ".", *, board: str, output: str | None = None,
                  dfu_util: str | None = None, sdk_home: Path | None = None,
                  coprocessor: bool = False, reset: bool = True, dry_run: bool = False
                  ) -> list[FlashStep]:
    spec = [("firmware", "firmware.bin")]
    if coprocessor:
        spec.append(("coprocessor", "firmware-M55_HE.bin"))
        spec.append(("coprocessor_romfs", "coprocessor-romfs.img"))
    spec.append(("romfs", "factory-romfs.img"))
    return _flash(project, board, spec, "flash-factory", output=output, dfu_util=dfu_util,
                  sdk_home=sdk_home, reset=reset, dry_run=dry_run)
