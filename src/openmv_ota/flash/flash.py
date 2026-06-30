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

import sys
from dataclasses import dataclass
from pathlib import Path

from openmv_ota.project import history

from . import arduino, device, dfu, imx, runner, tools
from .errors import FlashError
from .targets import FlashConfig, flash_config


def _mpremote(override: str | None) -> list[str]:
    """The argv prefix to run mpremote (a console script, also `python -m mpremote`)."""
    return [override] if override else [sys.executable, "-m", "mpremote"]


def _prepare(raw: dict, *, serial: str | None, enter_bootloader: bool, mpremote: str | None,
             dry_run: bool) -> str | None:
    """Get the running camera into its bootloader and return its USB serial (to pin dfu-util
    with ``-S`` when several boards are attached). A no-op for ``--dry-run`` or
    ``--in-bootloader``, or when no running camera is found (it's already in the bootloader)."""
    if dry_run or not enter_bootloader:
        return serial
    cam = device.select(raw, serial)             # raises if several match without --serial
    if cam is None:
        return serial                            # already in the bootloader / not attached
    device.reset(raw, cam, mpremote=_mpremote(mpremote))
    return cam.serial


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
               out_dir: Path, reset: bool, serial: str | None, dry_run: bool) -> list[FlashStep]:
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
        argv = dfu.download_argv(tool, cfg.usb, alt, f, reset=reset and i == last, serial=serial)
        if not dry_run:
            runner.run(argv)
        steps.append(FlashStep(artifact, f, alt, argv))
    return steps


def _dfu_flash(project: str, board: str, cfg: FlashConfig, spec: list[tuple[str, str]],
               action: str, *, output: str | None, dfu_util: str | None, sdk_home: Path | None,
               reset: bool, serial: str | None, dry_run: bool) -> list[FlashStep]:
    tool = _resolve_dfu_util(dfu_util, sdk_home, dry_run)
    steps = _dfu_steps(cfg, board, spec, tool, _output_dir(project, output), reset, serial,
                       dry_run)
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


def _loader(name: str, board: str) -> Path:
    """An i.MX flashloader binary, from the copies bundled in the package
    (``data/flashloaders/<board>/``). These are an internal crutch the user never handles --
    when the RT1062 moves to the same DFU bootloader as the other cameras this backend (and
    these files) goes away."""
    from importlib.resources import files
    return Path(str(files("openmv_ota").joinpath("data/flashloaders", board, name)))


def _imx_files(board: str, op: str, raw: dict, out_dir: Path) -> dict[str, Path]:
    sd, bl = raw["sdphost"], raw["blhost"]
    files = {"sdphost_loader": _loader(sd["loader"], board)}
    if op in ("firmware", "factory"):
        files["firmware"] = out_dir / ("%s-firmware.bin" % board)
    if op == "factory":
        files["blhost_loader"] = _loader(bl["sbl_loader"], board)
        files["romfs"] = out_dir / ("%s-factory-romfs.img" % board)
    elif op == "romfs":
        files["romfs"] = out_dir / ("%s-romfs.img" % board)
    for f in files.values():
        if not f.exists():
            raise FlashError("missing %s -- build the firmware/romfs first" % f)
    return files


def _sdk_python(blhost: str) -> str:
    """The python interpreter beside the spsdk tools (blhost is a wrapper that execs it) --
    used to run the in-process flashloader scan-wait."""
    return str(Path(blhost).parent / "python3")


def _imx_flash(project: str, op: str, board: str, cfg: FlashConfig, action: str, *,
               output: str | None, sdk_home: Path | None, dry_run: bool) -> list[imx.ImxStep]:
    out_dir = _output_dir(project, output)
    sdphost = _resolve_spsdk("sdphost", sdk_home, dry_run)
    blhost = _resolve_spsdk("blhost", sdk_home, dry_run)
    files = _imx_files(board, op, cfg.raw, out_dir)
    steps = imx.plan(op, cfg.raw, sdphost, blhost, _sdk_python(blhost), files)
    if not dry_run:
        for s in steps:
            runner.run(s.argv)
        history.record(project, action, board=board, steps=[s.label for s in steps])
    return steps


# --- arduino backend ------------------------------------------------------------------------

def _arduino_files(board: str, op: str, raw: dict, out_dir: Path) -> dict:
    files: dict = {}
    if op in ("firmware", "factory"):
        files["firmware"] = out_dir / ("%s-firmware.bin" % board)
    if op in ("romfs", "factory"):
        files["romfs"] = out_dir / ("%s-romfs.img" % board)
    if op == "factory":                              # wifi blobs ship in the output dir,
        files["wifi"] = [out_dir / w["file"] for w in raw["wifi"]]   # version-matched by build
    to_check = [files.get("firmware"), files.get("romfs"), *files.get("wifi", [])]
    for f in to_check:
        if f is not None and not f.exists():
            raise FlashError("missing %s -- build it first" % f)
    return files


def _arduino_flash(project: str, op: str, board: str, cfg: FlashConfig, action: str, *,
                   output: str | None, dfu_util: str | None, sdk_home: Path | None,
                   serial: str | None, dry_run: bool) -> list[arduino.ArduinoStep]:
    out_dir = _output_dir(project, output)
    tool = _resolve_dfu_util(dfu_util, sdk_home, dry_run)
    files = _arduino_files(board, op, cfg.raw, out_dir)
    steps = arduino.plan(op, cfg.raw, tool, files, serial=serial)
    if not dry_run:
        for s in steps:
            runner.run(s.argv)
        history.record(project, action, board=board, steps=[s.label for s in steps])
    return steps


# --- public verbs ---------------------------------------------------------------------------

def flash_firmware(project: str = ".", *, board: str, output: str | None = None,
                   dfu_util: str | None = None, sdk_home: Path | None = None,
                   reset: bool = True, enter_bootloader: bool = True, serial: str | None = None,
                   mpremote: str | None = None, dry_run: bool = False):
    cfg = flash_config(board)
    serial = _prepare(cfg.raw, serial=serial, enter_bootloader=enter_bootloader,
                      mpremote=mpremote, dry_run=dry_run)
    if cfg.backend == "imx":
        return _imx_flash(project, "firmware", board, cfg, "flash-firmware", output=output,
                          sdk_home=sdk_home, dry_run=dry_run)
    if cfg.backend == "arduino":
        return _arduino_flash(project, "firmware", board, cfg, "flash-firmware", output=output,
                              dfu_util=dfu_util, sdk_home=sdk_home, serial=serial, dry_run=dry_run)
    spec = [("firmware", "firmware.bin")]
    if cfg.has("coprocessor"):                   # AE3: the HE core ships with the firmware
        spec.append(("coprocessor", "firmware-M55_HE.bin"))
    return _dfu_flash(project, board, cfg, spec, "flash-firmware", output=output,
                      dfu_util=dfu_util, sdk_home=sdk_home, reset=reset, serial=serial,
                      dry_run=dry_run)


def flash_romfs(project: str = ".", *, board: str, output: str | None = None,
                dfu_util: str | None = None, sdk_home: Path | None = None,
                reset: bool = True, enter_bootloader: bool = True, serial: str | None = None,
                mpremote: str | None = None, dry_run: bool = False):
    cfg = flash_config(board)
    serial = _prepare(cfg.raw, serial=serial, enter_bootloader=enter_bootloader,
                      mpremote=mpremote, dry_run=dry_run)
    if cfg.backend == "imx":
        return _imx_flash(project, "romfs", board, cfg, "flash-romfs", output=output,
                          sdk_home=sdk_home, dry_run=dry_run)
    if cfg.backend == "arduino":
        return _arduino_flash(project, "romfs", board, cfg, "flash-romfs", output=output,
                              dfu_util=dfu_util, sdk_home=sdk_home, serial=serial, dry_run=dry_run)
    spec = [("romfs", "romfs.img")]
    return _dfu_flash(project, board, cfg, spec, "flash-romfs", output=output,
                      dfu_util=dfu_util, sdk_home=sdk_home, reset=reset, serial=serial,
                      dry_run=dry_run)


def flash_factory(project: str = ".", *, board: str, output: str | None = None,
                  dfu_util: str | None = None, sdk_home: Path | None = None,
                  reset: bool = True, enter_bootloader: bool = True, serial: str | None = None,
                  mpremote: str | None = None, dry_run: bool = False):
    cfg = flash_config(board)
    serial = _prepare(cfg.raw, serial=serial, enter_bootloader=enter_bootloader,
                      mpremote=mpremote, dry_run=dry_run)
    if cfg.backend == "imx":
        return _imx_flash(project, "factory", board, cfg, "flash-factory", output=output,
                          sdk_home=sdk_home, dry_run=dry_run)
    if cfg.backend == "arduino":
        return _arduino_flash(project, "factory", board, cfg, "flash-factory", output=output,
                              dfu_util=dfu_util, sdk_home=sdk_home, serial=serial, dry_run=dry_run)
    spec = [("firmware", "firmware.bin")]
    if cfg.has("coprocessor"):                   # AE3: HE core + its romfs, with the main image
        spec.append(("coprocessor", "firmware-M55_HE.bin"))
        spec.append(("coprocessor_romfs", "coprocessor-romfs.img"))
    spec.append(("romfs", "factory-romfs.img"))
    return _dfu_flash(project, board, cfg, spec, "flash-factory", output=output,
                      dfu_util=dfu_util, sdk_home=sdk_home, reset=reset, serial=serial,
                      dry_run=dry_run)


def _bootloader_bin(project: str, output: str | None, board: str) -> Path:
    f = _output_dir(project, output) / ("%s-bootloader.bin" % board)
    if not f.exists():
        raise FlashError("missing %s -- run `build firmware` first" % f)
    return f


def _bootloader_dfu(project, board, bl, f, *, dfu_util, sdk_home, serial, dry_run):
    tool = _resolve_dfu_util(dfu_util, sdk_home, dry_run)
    argv = dfu.bootloader_argv(tool, bl["usb"], int(bl["alt"]), bl["addr"], f, serial=serial)
    if not dry_run:
        runner.run(argv, tolerate_fail=True)         # the ST ROM doesn't ACK the final status
        history.record(project, "flash-bootloader", board=board,
                       files=[{"file": f.name, "addr": bl["addr"]}])
    return [FlashStep("bootloader", f, int(bl["alt"]), argv)]


def _resolve_cubeprog(sdk_home: Path | None, dry_run: bool) -> str:
    try:
        return tools.find_cubeprog(sdk_home)
    except FlashError:
        if not dry_run:
            raise
        return "STM32_Programmer_CLI"


def _bootloader_cubeprog(project, board, bl, f, *, sdk_home, dry_run):
    """N6: STM32CubeProgrammer flashes a FlashLayout.tsv that pairs the freshly-built
    ``bootloader.bin`` with the static FSBL/loader binaries (bundled). Stage them together (the
    tsv references each by name) and run CubeProgrammer over USB."""
    cube = _resolve_cubeprog(sdk_home, dry_run)
    argv = [cube, "-c", "port=USB1", "-d", bl["tsv"]]    # display/return; the real -d is staged
    if not dry_run:
        import shutil
        import tempfile
        from importlib.resources import files
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td)
            for name in [bl["tsv"], *bl["loaders"]]:     # bundled static layout + FSBL/loader
                shutil.copy(str(files("openmv_ota").joinpath("data/n6_bootloader", name)),
                            stage / name)
            shutil.copy(str(f), stage / "bootloader.bin")   # the name the tsv references
            runner.run([cube, "-c", "port=USB1", "-d", str(stage / bl["tsv"])])
        history.record(project, "flash-bootloader", board=board, files=[{"file": f.name}])
    return [FlashStep("bootloader", f, 0, argv)]


def flash_bootloader(project: str = ".", *, board: str, output: str | None = None,
                     dfu_util: str | None = None, sdk_home: Path | None = None,
                     serial: str | None = None, dry_run: bool = False):
    """Flash the board's bootloader. Unlike firmware/romfs, this can't go through the OpenMV
    bootloader (it protects itself) -- the board must be in its **system** ROM DFU, entered by
    hand (BOOT0/jumper) on a programmed camera (a virgin one is there already). So there's no
    auto-reset; we print the board's instructions and wait for the system-DFU device."""
    cfg = flash_config(board)
    bl = cfg.raw.get("bootloader")
    if not bl:
        raise FlashError("board %r has no bootloader to flash with this tool" % board)
    backend = bl["backend"]
    if backend not in ("dfu", "cubeprog"):           # RT (factory) / AE3 (alif)
        raise FlashError("bootloader flashing for %r isn't available here: %s"
                         % (board, bl.get("note", "unsupported")))
    f = _bootloader_bin(project, output, board)
    print(bl["instructions"], file=sys.stderr)       # the manual system-DFU entry (BOOT0/jumper)
    if backend == "dfu":
        return _bootloader_dfu(project, board, bl, f, dfu_util=dfu_util, sdk_home=sdk_home,
                               serial=serial, dry_run=dry_run)
    return _bootloader_cubeprog(project, board, bl, f, sdk_home=sdk_home, dry_run=dry_run)
