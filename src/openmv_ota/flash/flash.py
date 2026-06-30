"""Orchestrate flashing a board's built artifacts to their partitions.

``flash firmware`` writes the firmware image; ``flash romfs`` the app image; ``flash
factory`` the manufacturing program. The backend is chosen by the board's ``flash`` block:

* **dfu** -- resolve every artifact + alt *before* writing anything (fail fast), and reset
  the board only after the final write so a multi-step flash stays in the bootloader between
  steps. A step is ``(logical-artifact, default-filename-suffix)``; the file is
  ``<board>-<suffix>`` unless the board's ``flash.file`` map overrides it (the AE3's per-core
  ``firmware-M55_HP.bin``).
* **imx** (RT1060) -- run the sdphost/blhost sequence (see ``flash.imx``), polling for the
  flashloader to come up after the jump before the blhost writes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from openmv_ota.project import history

from . import dfu, imx, runner, tools
from .errors import FlashError
from .targets import FlashConfig, flash_config

_PROBE_ATTEMPTS = 10             # poll get-property up to this many times after the jump
_PROBE_SETTLE_S = 2.0            # let the flashloader enumerate before the first poll
_PROBE_DELAY_S = 1.0            # between polls


@dataclass(frozen=True)
class FlashStep:                 # a dfu step (an imx step is flash.imx.ImxStep)
    artifact: str
    file: Path
    alt: int
    argv: list[str]


def _output_dir(project: str, output: str | None) -> Path:
    return Path(output) if output else Path(project) / "build"


# --- dfu backend ----------------------------------------------------------------------------

def _resolve_dfu_util(dfu_util: str | None, sdk_home: Path | None, dry_run: bool) -> str:
    try:
        return tools.find_dfu_util(dfu_util, sdk_home)
    except FlashError:
        if not dry_run:                              # dry-run can show the command even
            raise                                    # when dfu-util isn't installed
        return dfu_util or "dfu-util"


def _dfu_steps(cfg: FlashConfig, board: str, spec: list[tuple[str, str]], tool: str,
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


def _dfu_flash(project: str, board: str, cfg: FlashConfig, spec: list[tuple[str, str]],
               action: str, *, output: str | None, dfu_util: str | None, sdk_home: Path | None,
               reset: bool, dry_run: bool) -> list[FlashStep]:
    tool = _resolve_dfu_util(dfu_util, sdk_home, dry_run)
    steps = _dfu_steps(cfg, board, spec, tool, _output_dir(project, output), reset, dry_run)
    if not dry_run:
        history.record(project, action, board=board,
                       files=[{"file": s.file.name, "alt": s.alt} for s in steps])
    return steps


# --- imx backend ----------------------------------------------------------------------------

def _resolve_spsdk(name: str, sdk_home: Path | None, dry_run: bool) -> str:
    try:
        return tools.find_spsdk(name, sdk_home)
    except FlashError:
        if not dry_run:
            raise
        return name


def _imx_files(board: str, op: str, raw: dict, out_dir: Path, ld_dir: Path) -> dict[str, Path]:
    sd, bl = raw["sdphost"], raw["blhost"]
    files = {"sdphost_loader": ld_dir / sd["loader"]}
    if op in ("firmware", "factory"):
        files["firmware"] = out_dir / ("%s-firmware.bin" % board)
    if op == "factory":
        files["blhost_loader"] = ld_dir / bl["sbl_loader"]
        files["romfs"] = out_dir / ("%s-factory-romfs.img" % board)
    elif op == "romfs":
        files["romfs"] = out_dir / ("%s-romfs.img" % board)
    for f in files.values():
        if not f.exists():
            raise FlashError("missing %s -- build the firmware/romfs first; the flashloader "
                             ".bin files ship with the firmware (or pass --flashloader-dir)" % f)
    return files


def _poll(argv: list[str]) -> None:
    """Wait for the flashloader to answer after the SDP jump (it re-enumerates as a new
    USB device): settle, then retry ``get-property`` until it responds."""
    time.sleep(_PROBE_SETTLE_S)
    for attempt in range(_PROBE_ATTEMPTS):
        try:
            runner.run(argv)
            return
        except FlashError:
            if attempt + 1 == _PROBE_ATTEMPTS:
                raise FlashError("i.MX flashloader never came up (no get-property response "
                                 "after %d tries)" % _PROBE_ATTEMPTS) from None
            time.sleep(_PROBE_DELAY_S)


def _execute_imx(steps: list[imx.ImxStep]) -> None:
    for s in steps:
        if s.probe:
            _poll(s.argv)
        else:
            runner.run(s.argv)


def _imx_flash(project: str, op: str, board: str, cfg: FlashConfig, action: str, *,
               output: str | None, sdk_home: Path | None, flashloader_dir: str | None,
               dry_run: bool) -> list[imx.ImxStep]:
    out_dir = _output_dir(project, output)
    ld_dir = Path(flashloader_dir) if flashloader_dir else out_dir
    sdphost = _resolve_spsdk("sdphost", sdk_home, dry_run)
    blhost = _resolve_spsdk("blhost", sdk_home, dry_run)
    files = _imx_files(board, op, cfg.raw, out_dir, ld_dir)
    steps = imx.plan(op, cfg.raw, sdphost, blhost, files)
    if not dry_run:
        _execute_imx(steps)
        history.record(project, action, board=board, steps=[s.label for s in steps])
    return steps


# --- public verbs ---------------------------------------------------------------------------

def flash_firmware(project: str = ".", *, board: str, output: str | None = None,
                   dfu_util: str | None = None, sdk_home: Path | None = None,
                   flashloader_dir: str | None = None, coprocessor: bool = False,
                   reset: bool = True, dry_run: bool = False):
    cfg = flash_config(board)
    if cfg.backend == "imx":
        return _imx_flash(project, "firmware", board, cfg, "flash-firmware", output=output,
                          sdk_home=sdk_home, flashloader_dir=flashloader_dir, dry_run=dry_run)
    spec = [("firmware", "firmware.bin")]
    if coprocessor:
        spec.append(("coprocessor", "firmware-M55_HE.bin"))
    return _dfu_flash(project, board, cfg, spec, "flash-firmware", output=output,
                      dfu_util=dfu_util, sdk_home=sdk_home, reset=reset, dry_run=dry_run)


def flash_romfs(project: str = ".", *, board: str, output: str | None = None,
                dfu_util: str | None = None, sdk_home: Path | None = None,
                flashloader_dir: str | None = None, reset: bool = True, dry_run: bool = False):
    cfg = flash_config(board)
    if cfg.backend == "imx":
        return _imx_flash(project, "romfs", board, cfg, "flash-romfs", output=output,
                          sdk_home=sdk_home, flashloader_dir=flashloader_dir, dry_run=dry_run)
    spec = [("romfs", "romfs.img")]
    return _dfu_flash(project, board, cfg, spec, "flash-romfs", output=output,
                      dfu_util=dfu_util, sdk_home=sdk_home, reset=reset, dry_run=dry_run)


def flash_factory(project: str = ".", *, board: str, output: str | None = None,
                  dfu_util: str | None = None, sdk_home: Path | None = None,
                  flashloader_dir: str | None = None, coprocessor: bool = False,
                  reset: bool = True, dry_run: bool = False):
    cfg = flash_config(board)
    if cfg.backend == "imx":
        return _imx_flash(project, "factory", board, cfg, "flash-factory", output=output,
                          sdk_home=sdk_home, flashloader_dir=flashloader_dir, dry_run=dry_run)
    spec = [("firmware", "firmware.bin")]
    if coprocessor:
        spec.append(("coprocessor", "firmware-M55_HE.bin"))
        spec.append(("coprocessor_romfs", "coprocessor-romfs.img"))
    spec.append(("romfs", "factory-romfs.img"))
    return _dfu_flash(project, board, cfg, spec, "flash-factory", output=output,
                      dfu_util=dfu_util, sdk_home=sdk_home, reset=reset, dry_run=dry_run)
