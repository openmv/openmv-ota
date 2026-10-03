"""Orchestrate flashing a board's built artifacts to their partitions.

``flash firmware`` writes the firmware image; ``flash romfs`` the app image; ``flash
factory`` the manufacturing program. The backend is chosen by the board's ``flash`` block:

* **dfu** -- resolve every artifact + alt *before* writing anything (fail fast), and reset
  the board only after the final write so a multi-step flash stays in the bootloader between
  steps. A step is ``(logical-artifact, default-filename-suffix)``; the file is
  ``<board>-<suffix>`` unless the board's ``flash.file`` map overrides it (the AE3's per-core
  ``firmware-M55_HP.bin``).
* **imx** (RT1060) -- blhost against the camera's resident secure bootloader, entered and held
  by the catcher; ``factory --provision`` is the blank-board path, the sdphost ROM sequence first
  (see ``flash.imx``).

EVERY PRECHECK BEFORE THE RESET. Each verb resolves its tool and every artifact first, and only
then resets the running camera into its bootloader: a missing dfu-util or an unbuilt image used to
be discovered with the board already sitting in DFU (a Portenta then stays there until it is
power-cycled).

THE FIRST DFU WRITE WAITS FOR A *LISTED* DEVICE, NOT ``dfu-util -w``. ``-w`` opens the device the
instant libusb sees it, before the bootloader has finished enumerating -- measured on the N6 as
"Failed to retrieve language identifiers" then "Cannot set alternate interface:
LIBUSB_ERROR_OTHER" (exit 74), 3 of 3 tries, on two dfu-util 0.11 builds, and the N6 boots back to
its app ~1.8 s after an idle DFU entry. So: poll ``dfu-util -l`` for the vid:pid, settle 300 ms,
then download without ``-w``. Later steps keep ``-w``: once a download has started the board stays
in DFU.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

from openmv_ota.project import history

from . import alif, arduino, device, dfu, imx, inventory, runner, tools
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
    if raw.get("backend") == "imx":              # imx: the resident-SBL catcher (flash.imx) must arm
        return cam.serial                        # BEFORE the reset, so _imx_flash owns the reset
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


_DFU_WAIT_S = 60          # a DFU device to appear (covers a bootloader entered by hand, or an nRST catch)
_DFU_POLL_S = 0.1         # between `dfu-util -l` scans
_DFU_SETTLE_S = 0.3       # once listed, let the bootloader finish enumerating before the first write


def _dfu_listed(tool: str, usb: str) -> bool:
    """Whether ``dfu-util -l`` lists a DFU device with id ``usb`` right now."""
    try:
        out = runner.output([tool, "-l"])
    except FlashError:
        return False                             # a scan that failed lists nothing
    return ("[%s]" % usb.lower()) in out.lower()


def _await_dfu(tool: str, usb: str, timeout: float = _DFU_WAIT_S) -> bool:
    """Poll until the DFU device ``usb`` is listed, then settle; False on timeout. The bounded
    replacement for the first step's ``dfu-util -w`` -- see the module docstring."""
    deadline = time.monotonic() + timeout
    while True:
        if _dfu_listed(tool, usb):
            time.sleep(_DFU_SETTLE_S)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_DFU_POLL_S)


def _no_wait(argv: list[str]) -> list[str]:
    """``argv`` without dfu-util's ``-w``: the first step runs after _await_dfu has seen the device."""
    return [a for a in argv if a != "-w"]


def _ensure_dfu(tool: str, usb: str, board: str) -> None:
    if not _await_dfu(tool, usb):
        raise FlashError("%s: no DFU device (%s) appeared within %d s -- is it attached? Put it in "
                         "its bootloader by hand and rerun with --in-bootloader"
                         % (board, usb, _DFU_WAIT_S))


# --- dfu backend ----------------------------------------------------------------------------

def _resolve_dfu_util(dfu_util: str | None, sdk_home: Path | None, dry_run: bool) -> str:
    try:
        return tools.find_dfu_util(dfu_util, sdk_home)
    except FlashError:
        if not dry_run:                              # dry-run can show the command even
            raise                                    # when dfu-util isn't installed
        return dfu_util or "dfu-util"


def _dfu_steps(cfg: FlashConfig, board: str, spec: list[tuple[str, str]], tool: str,
               out_dir: Path, reset: bool) -> list[FlashStep]:
    """Resolve every artifact + alt (raising before anything runs) and build each step's argv."""
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
        # Do NOT pin dfu-util with -S here: an OpenMV board advertises a DIFFERENT usb serial in DFU
        # than at runtime (the UID is byte-reversed between the two), so -S <runtime-serial> matches
        # nothing and dfu-util -w waits forever. device.reset already put ONLY the selected board into
        # DFU, so the vid:pid filter alone targets it -- the same thing the IDE does. (--serial still
        # picks WHICH board to reset, up in _prepare/device.select; it just can't pin the DFU device.)
        argv = dfu.download_argv(tool, cfg.usb, alt, f, reset=reset and i == last)
        steps.append(FlashStep(artifact, f, alt, _no_wait(argv) if i == 0 else argv))
    return steps


def _dfu_flash(project: str, board: str, cfg: FlashConfig, spec: list[tuple[str, str]],
               action: str, *, output: str | None, dfu_util: str | None, sdk_home: Path | None,
               reset: bool, serial: str | None, enter_bootloader: bool, mpremote: str | None,
               dry_run: bool) -> list[FlashStep]:
    tool = _resolve_dfu_util(dfu_util, sdk_home, dry_run)
    steps = _dfu_steps(cfg, board, spec, tool, _output_dir(project, output), reset)
    if dry_run:
        return steps
    _prepare(cfg.raw, serial=serial, enter_bootloader=enter_bootloader, mpremote=mpremote,
             dry_run=dry_run)                    # only now: every precheck has passed
    _ensure_dfu(tool, cfg.usb, board)
    for s in steps:
        runner.run(s.argv)
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
    files: dict[str, Path] = {}
    if op in imx.ROM_OPS:                            # recovery over SDP: the RAM flashloader + the
        files["sdphost_loader"] = _loader(sd["loader"], board)   # bundled secure bootloader (SBL).
        files["blhost_loader"] = _loader(bl["sbl_loader"], board)   # the resident-SBL ops need neither.
    if op in ("firmware", "factory", "provision"):
        files["firmware"] = out_dir / ("%s-firmware.bin" % board)
    if op in ("factory", "provision"):
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


def _await_line(proc, marker: str, timeout: float) -> bool:
    """Read ``proc``'s stdout until a line contains ``marker`` (True), the process exits (True iff
    it exited 0 -- the marker line is read before EOF), or ``timeout`` elapses (False). Uses select
    so a silent-but-alive process can't block past the timeout (the catcher warms spsdk silently
    before READY, then goes quiet between READY and CLAIMED)."""
    import select
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([proc.stdout], [], [], 0.3)
        if not r:                                # nothing yet (spsdk still importing / SBL not up)
            continue
        line = proc.stdout.readline()
        if line == "":                           # a closed pipe -> the process has exited
            return proc.wait() == 0
        if marker in line:
            return True
    return False


def _imx_catch_and_reset(raw: dict, python3: str, mpremote: str | None, serial: str | None) -> None:
    """Enter + hold the resident SBL the IDE's imxArmCatcher way: ARM the catcher (wait for READY --
    spsdk warmed + scanning), THEN reset the running camera into the SBL (machine.bootloader drops
    the USB-CDC, so fire it and don't block), and let the armed catcher CLAIM the SBL the instant it
    enumerates -- holding it against the ~1 s idle timeout so the region blhost ops that follow land.
    A passive post-reset scan misses that window; this is why the automatable imx path resets HERE
    rather than in _prepare. Raises FlashError on arm/claim timeout."""
    import subprocess
    catcher = subprocess.Popen(imx.catcher_argv(python3, raw["blhost"]["usb"], "claim"),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        if not _await_line(catcher, "READY", 45):
            raise FlashError("i.MX: the resident-SBL catcher never armed (spsdk import failed?)")
        cam = device.select(raw, serial)         # the running camera to reset (None -> already in SBL)
        if cam is not None:
            # `exec machine.bootloader()`, NOT mpremote's `bootloader` subcommand -- the same
            # distinction device.reset() documents for the DFU boards. The subcommand does more
            # REPL work to get there, and this call is made in exactly the state where that extra
            # work is least likely to succeed: the harness reaches for the SBL to REPAIR a corrupt
            # /flash, having just failed to write it (OSError 5). A direct exec asks the firmware
            # for the one thing needed. Fire and forget either way -- the call tears down the
            # USB-CDC mid-exec, so there is no exit status worth waiting for; the CATCHER decides
            # whether it worked.
            subprocess.Popen([*_mpremote(mpremote), "connect", cam.port,
                              "exec", "import machine; machine.bootloader()"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _await_line(catcher, "CLAIMED", 45):
            raise FlashError("i.MX: the resident SBL did not enumerate / could not be claimed (a "
                             "blank board has no SBL yet: provision it with `flash factory "
                             "--provision`)")
    finally:
        if catcher.poll() is None:
            catcher.terminate()
            try:
                catcher.wait(timeout=5)
            except Exception:                    # pragma: no cover  (terminate() practically always reaps)
                catcher.kill()


def _imx_flash(project: str, op: str, board: str, cfg: FlashConfig, action: str, *,
               output: str | None, sdk_home: Path | None, dry_run: bool,
               mpremote: str | None = None, serial: str | None = None,
               enter_bootloader: bool = True) -> list[imx.ImxStep]:
    out_dir = _output_dir(project, output)
    recovery = op in imx.ROM_OPS
    sdphost = _resolve_spsdk("sdphost", sdk_home, dry_run) if recovery else ""
    blhost = _resolve_spsdk("blhost", sdk_home, dry_run)
    python3 = _sdk_python(blhost)
    files = _imx_files(board, op, cfg.raw, out_dir)
    steps = imx.plan(op, cfg.raw, sdphost, blhost, python3, files)
    if not dry_run:
        if not recovery:                         # automatable: enter + CLAIM the resident SBL first
            serial = _prepare(cfg.raw, serial=serial, enter_bootloader=enter_bootloader,
                              mpremote=mpremote, dry_run=dry_run)
            _imx_catch_and_reset(cfg.raw, python3, mpremote, serial)
        for s in steps:
            runner.run(s.argv)
        history.record(project, action, board=board, steps=[s.label for s in steps])
    return steps


# --- arduino backend ------------------------------------------------------------------------

def _arduino_files(board: str, op: str, raw: dict, out_dir: Path) -> dict:
    files: dict = {}
    if op in ("firmware", "factory"):
        files["firmware"] = out_dir / ("%s-firmware.bin" % board)
    if op == "factory":                              # the dual-slot image `build factory-romfs` writes
        files["romfs"] = out_dir / ("%s-factory-romfs.img" % board)
    elif op == "romfs":
        files["romfs"] = out_dir / ("%s-romfs.img" % board)
    if op == "factory":                              # wifi blobs ship in the output dir,
        files["wifi"] = [out_dir / w["file"] for w in raw["wifi"]]   # version-matched by build
    to_check = [files.get("firmware"), files.get("romfs"), *files.get("wifi", [])]
    for f in to_check:
        if f is not None and not f.exists():
            raise FlashError("missing %s -- build it first" % f)
    return files


_TOUCH_TRIES = 2          # 1200-baud touches before giving up on automatic DFU entry
_TOUCH_DFU_WAIT_S = 20    # MCUboot's DFU device enumerates within a few seconds of a touch that took
_REBOOT_WAIT_S = 60       # a touch that did NOT take reboots the app (the Nicla is back in ~33 s)
_PORT_POLL_S = 0.5

_DOUBLE_TAP = ("double-tap the board's reset button to enter its bootloader (the LED pulses), "
               "then rerun with --in-bootloader")


def _await_camera(raw: dict, serial: str | None, timeout: float) -> device.Camera | None:
    """The running camera, polled for up to ``timeout`` s (0 = look once)."""
    deadline = time.monotonic() + timeout
    while True:
        cam = device.select(raw, serial)
        if cam is not None or time.monotonic() >= deadline:
            return cam
        time.sleep(_PORT_POLL_S)


def _arduino_enter(board: str, raw: dict, tool: str, serial: str | None,
                   enter_bootloader: bool, first_wait: float = 0) -> None:
    """Get an Arduino board into its MCUboot DFU bootloader, or raise saying how to by hand.

    The 1200-baud touch is answered by the firmware's USB stack, which schedules the jump 250 ms
    later -- and an app with an armed short watchdog (100 ms) is reset by it first, so the board
    reboots into the app instead (measured on the Nicla and the Giga). Nothing on the host can
    feed that watchdog, and mpremote's machine.bootloader() fares no better (its Ctrl-C stops the
    app, the watchdog then bites). What the host CAN do is try again once the app is back: a
    second touch lands before an app that arms its watchdog late. Bounded, then a clear message."""
    usb = raw["usb"]
    if enter_bootloader:
        for attempt in range(_TOUCH_TRIES):
            cam = _await_camera(raw, serial, first_wait if attempt == 0 else _REBOOT_WAIT_S)
            if cam is None:
                break                            # not running: already in DFU, or detached
            device.reset(raw, cam, mpremote=[])  # the 1200-baud touch
            if _await_dfu(tool, usb, _TOUCH_DFU_WAIT_S):
                return
            print("warning: %s did not enter DFU after the 1200-baud touch (attempt %d/%d)"
                  % (board, attempt + 1, _TOUCH_TRIES), file=sys.stderr)
        else:
            raise FlashError("%s did not enter its DFU bootloader after %d 1200-baud touches (an "
                             "app with an armed watchdog reboots before the touch lands): %s"
                             % (board, _TOUCH_TRIES, _DOUBLE_TAP))
    if not _await_dfu(tool, usb):
        raise FlashError("%s: no DFU device (%s) appeared within %d s -- %s"
                         % (board, usb, _DFU_WAIT_S, _DOUBLE_TAP))


def _arduino_run(steps: list[arduino.ArduinoStep]) -> None:
    for s in steps:
        runner.run(s.argv)


def _arduino_flash(project: str, op: str, board: str, cfg: FlashConfig, action: str, *,
                   output: str | None, dfu_util: str | None, sdk_home: Path | None,
                   serial: str | None, enter_bootloader: bool, dry_run: bool
                   ) -> list[arduino.ArduinoStep]:
    out_dir = _output_dir(project, output)
    tool = _resolve_dfu_util(dfu_util, sdk_home, dry_run)
    files = _arduino_files(board, op, cfg.raw, out_dir)
    steps = arduino.plan(op, cfg.raw, tool, files, serial=serial)
    if dry_run:
        return steps
    _arduino_enter(board, cfg.raw, tool, serial, enter_bootloader)   # every precheck has passed
    try:
        _arduino_run(steps)
    except FlashError as e:
        # A WEDGED DFU DEVICE, retried once from a clean entry. MCUboot's DfuSe stalls every
        # control transfer once it is in dfuERROR (dfuDNBUSY / LIBUSB_ERROR_PIPE), so the same
        # write can only fail again -- seen on the Giga's first write after a manual DFU entry.
        # Leave DFU (the app boots), re-enter with a touch, write everything again.
        print("warning: %s: %s -- leaving DFU and retrying once from a fresh entry" % (board, e),
              file=sys.stderr)
        runner.run(arduino.leave_argv(tool, cfg.raw["usb"]), tolerate_fail=True)
        _arduino_enter(board, cfg.raw, tool, serial, True, first_wait=_REBOOT_WAIT_S)
        _arduino_run(steps)
    history.record(project, action, board=board, steps=[s.label for s in steps])
    return steps


# --- public verbs ---------------------------------------------------------------------------

def flash_firmware(project: str = ".", *, board: str, output: str | None = None,
                   dfu_util: str | None = None, sdk_home: Path | None = None,
                   reset: bool = True, enter_bootloader: bool = True, serial: str | None = None,
                   mpremote: str | None = None, dry_run: bool = False):
    cfg = flash_config(board)
    if cfg.backend == "imx":
        return _imx_flash(project, "firmware", board, cfg, "flash-firmware", output=output,
                          sdk_home=sdk_home, dry_run=dry_run, mpremote=mpremote, serial=serial,
                          enter_bootloader=enter_bootloader)
    if cfg.backend == "arduino":
        return _arduino_flash(project, "firmware", board, cfg, "flash-firmware", output=output,
                              dfu_util=dfu_util, sdk_home=sdk_home, serial=serial,
                              enter_bootloader=enter_bootloader, dry_run=dry_run)
    spec = [("firmware", "firmware.bin")]
    if cfg.has("coprocessor"):                   # AE3: the HE core ships with the firmware
        spec.append(("coprocessor", "firmware-M55_HE.bin"))
    return _dfu_flash(project, board, cfg, spec, "flash-firmware", output=output,
                      dfu_util=dfu_util, sdk_home=sdk_home, reset=reset, serial=serial,
                      enter_bootloader=enter_bootloader, mpremote=mpremote, dry_run=dry_run)


def flash_romfs(project: str = ".", *, board: str, output: str | None = None,
                dfu_util: str | None = None, sdk_home: Path | None = None,
                reset: bool = True, enter_bootloader: bool = True, serial: str | None = None,
                mpremote: str | None = None, dry_run: bool = False):
    cfg = flash_config(board)
    if cfg.backend == "imx":
        return _imx_flash(project, "romfs", board, cfg, "flash-romfs", output=output,
                          sdk_home=sdk_home, dry_run=dry_run, mpremote=mpremote, serial=serial,
                          enter_bootloader=enter_bootloader)
    if cfg.backend == "arduino":
        return _arduino_flash(project, "romfs", board, cfg, "flash-romfs", output=output,
                              dfu_util=dfu_util, sdk_home=sdk_home, serial=serial,
                              enter_bootloader=enter_bootloader, dry_run=dry_run)
    spec = [("romfs", "romfs.img")]
    return _dfu_flash(project, board, cfg, spec, "flash-romfs", output=output,
                      dfu_util=dfu_util, sdk_home=sdk_home, reset=reset, serial=serial,
                      enter_bootloader=enter_bootloader, mpremote=mpremote, dry_run=dry_run)


def flash_factory(project: str = ".", *, board: str, output: str | None = None,
                  dfu_util: str | None = None, sdk_home: Path | None = None,
                  reset: bool = True, enter_bootloader: bool = True, serial: str | None = None,
                  mpremote: str | None = None, provision: bool = False, dry_run: bool = False):
    """Flash the manufacturing program: firmware + the dual-slot factory romfs (+ the AE3's HE
    core and coprocessor romfs, + the Arduino boards' wifi blobs).

    On the i.MX RT1062 this goes through the camera's RESIDENT secure bootloader, the same no-
    jumper path ``flash firmware``/``romfs`` take: a camera that ships from the factory already
    has it, so a board running OpenMV firmware (or sitting in the SBL) is all it needs.
    ``provision=True`` is the other thing a factory does to a BLANK board -- load a flashloader
    over the i.MX ROM's serial download (SBL boot jumper), write the flash-config block and the
    secure bootloader, the firmware and romfs, and burn the boot e-fuse. It exists only on i.MX."""
    cfg = flash_config(board)
    if provision and cfg.backend != "imx":
        raise FlashError("--provision is the i.MX ROM provisioning path (FCB, secure bootloader, "
                         "boot e-fuse); board %r has none -- run `flash factory` without it" % board)
    if cfg.backend == "imx":
        if provision:
            print(cfg.raw["bootloader"]["instructions"], file=sys.stderr)   # the SBL jumper
            return _imx_flash(project, "provision", board, cfg, "flash-factory", output=output,
                              sdk_home=sdk_home, dry_run=dry_run)
        return _imx_flash(project, "factory", board, cfg, "flash-factory", output=output,
                          sdk_home=sdk_home, dry_run=dry_run, mpremote=mpremote, serial=serial,
                          enter_bootloader=enter_bootloader)
    if cfg.backend == "arduino":
        return _arduino_flash(project, "factory", board, cfg, "flash-factory", output=output,
                              dfu_util=dfu_util, sdk_home=sdk_home, serial=serial,
                              enter_bootloader=enter_bootloader, dry_run=dry_run)
    spec = [("firmware", "firmware.bin")]
    if cfg.has("coprocessor"):                   # AE3: HE core + its romfs, with the main image
        spec.append(("coprocessor", "firmware-M55_HE.bin"))
        spec.append(("coprocessor_romfs", "coprocessor-romfs.img"))
    spec.append(("romfs", "factory-romfs.img"))
    return _dfu_flash(project, board, cfg, spec, "flash-factory", output=output,
                      dfu_util=dfu_util, sdk_home=sdk_home, reset=reset, serial=serial,
                      enter_bootloader=enter_bootloader, mpremote=mpremote, dry_run=dry_run)


@dataclass(frozen=True)
class EraseStep:
    label: str
    argv: list[str]


_ERASE_SECTOR = 4096            # FLASH_SECTOR_ERASE: a sector of zeros invalidates the filesystem


def flash_erase(project: str = ".", *, board: str, dfu_util: str | None = None,
                sdk_home: Path | None = None, reset: bool = True, enter_bootloader: bool = True,
                serial: str | None = None, mpremote: str | None = None, romfs: bool = False,
                dry_run: bool = False):
    """Erase a board's onboard filesystem (the user disk) so the firmware reformats a clean one
    on the next boot, mirroring the IDE's "Erase Onboard Data Flash". On dfu/arduino boards that
    means downloading a sector of zeros to the filesystem alt/address (the IDE's eraseCommands);
    on the RT1060 (imx) it's a blhost ``flash-erase-region`` of the disk's MBR sector.

    ``romfs=True`` instead erases the whole OTA romfs region (both slots), blanking any installed
    image so boot.py finds no valid trailer and the firmware-resident recovery runs -- imx-only (the
    block-device boards); other backends have no single romfs region to erase. The retired Nanos
    are refused."""
    cfg = flash_config(board)                        # refuses the retired Nanos
    if cfg.backend == "imx":                          # RT1060: erase disk MBR (or the romfs region) via blhost
        return _imx_flash(project, "erase_romfs" if romfs else "erase", board, cfg, "flash-erase",
                          output=None, sdk_home=sdk_home, dry_run=dry_run, mpremote=mpremote,
                          serial=serial, enter_bootloader=enter_bootloader)
    if romfs:
        raise FlashError("--romfs erase is only supported on the imx backend (board %r is %s)"
                         % (board, cfg.backend))
    targets = cfg.raw.get("erase")
    if not targets:
        raise FlashError("board %r has no erase target configured" % board)
    tool = _resolve_dfu_util(dfu_util, sdk_home, dry_run)
    last = len(targets) - 1

    def step(t: dict, i: int, f: Path) -> EraseStep:
        argv = dfu.erase_argv(tool, cfg.usb, t, f, leave=reset and i == last, serial=serial)
        return EraseStep("erase alt %s" % t["alt"], _no_wait(argv) if i == 0 else argv)

    if dry_run:
        return [step(t, i, Path("<zeros>")) for i, t in enumerate(targets)]
    if cfg.backend == "arduino":                      # every precheck has passed: now enter DFU
        _arduino_enter(board, cfg.raw, tool, serial, enter_bootloader)
    else:
        serial = _prepare(cfg.raw, serial=serial, enter_bootloader=enter_bootloader,
                          mpremote=mpremote, dry_run=dry_run)
        _ensure_dfu(tool, cfg.usb, board)
    import tempfile
    steps: list[EraseStep] = []
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "erase.bin"
        f.write_bytes(b"\x00" * _ERASE_SECTOR)
        for i, t in enumerate(targets):
            s = step(t, i, f)
            runner.run(s.argv)
            steps.append(s)
    history.record(project, "flash-erase", board=board,
                   files=[{"alt": t["alt"]} for t in targets])
    return steps


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


# --- alif backend (AE3 bootloader) ----------------------------------------------------------

def _alif_toolkit(project: str, bl: dict, dry_run: bool) -> str:
    try:
        return tools.find_alif_toolkit(project, bl["toolkit"])
    except FlashError:
        if not dry_run:
            raise
        return str(Path(project) / bl["toolkit"])


def _alif_files(board: str, bl: dict, out_dir: Path) -> dict[str, Path]:
    files = {i["file"]: out_dir / ("%s-%s" % (board, i["file"])) for i in bl["images"]}
    for f in files.values():
        if not f.exists():
            raise FlashError("missing %s -- run `build firmware` first" % f)
    return files


def _alif_se_uart(bl: dict, dry_run: bool) -> alif.SeUart:
    if dry_run:                                  # don't require hardware to show the commands
        v = bl["variants"][0]
        return alif.SeUart("<se-uart-port>", v["cfg_part"], v["name"])
    return alif.find_se_uart(bl["variants"], device._comports())


def _alif_replug(board: str) -> None:
    print("\nUnplug and replug the %s now -- the system-package update needs a power cycle -- "
          "re-enter SE-UART maintenance mode, then press Enter to continue..." % board,
          file=sys.stderr)
    input()


def _alif_flash(project: str, board: str, bl: dict, *, output: str | None,
                dry_run: bool) -> list[alif.AlifStep]:
    """Always update the system package first (it's coupled to the bootloader), have the
    operator power-cycle the board (mandatory on a virgin part), re-find the SE-UART port, then
    write the SBL bootloader + padded TOC to MRAM."""
    out_dir = _output_dir(project, output)
    toolkit = _alif_toolkit(project, bl, dry_run)
    rev = bl["cfg_rev"]
    images = alif.images_arg(bl["images"], _alif_files(board, bl, out_dir))   # fail fast
    se = _alif_se_uart(bl, dry_run)
    usp = alif.update_system_package_argv(sys.executable, toolkit, se, rev)
    if not dry_run:
        runner.run(usp)
        _alif_replug(board)
        se = _alif_se_uart(bl, dry_run)          # the port may re-enumerate after the replug
    write = alif.write_bootloader_argv(sys.executable, toolkit, se, rev, images)
    steps = [alif.AlifStep("update system package", usp),
             alif.AlifStep("write bootloader", write)]
    if not dry_run:
        runner.run(write)
        history.record(project, "flash-bootloader", board=board,
                       steps=[s.label for s in steps])
    return steps


def scan_devices(*, dfu_util: str | None = None,
                 sdk_home: Path | None = None) -> list[inventory.Device]:
    """Enumerate every connected, identifiable board and the state it's in. Each scanner
    degrades on its own: serial always runs; the DFU scan is skipped (with a note) if dfu-util
    is absent; the i.MX scan is skipped quietly if the SDK's spsdk isn't reachable."""
    devices = inventory.serial_devices()
    try:
        devices += inventory.dfu_devices(tools.find_dfu_util(dfu_util, sdk_home))
    except FlashError:
        print("warning: dfu-util not found -- skipping the DFU/recovery scan", file=sys.stderr)
    try:
        devices += inventory.imx_devices(_sdk_python(tools.find_spsdk("blhost", sdk_home)))
    except FlashError:
        pass                                         # the i.MX scan needs the SDK's spsdk
    return sorted(devices, key=lambda d: (d.board, d.state, d.where))


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
    if backend not in ("dfu", "cubeprog", "imx", "alif"):
        raise FlashError("bootloader flashing for %r isn't available here: %s"
                         % (board, bl.get("note", "unsupported")))
    print(bl["instructions"], file=sys.stderr)       # the manual recovery entry (BOOT0/SBL jumper)
    if backend == "imx":                             # RT: the SDP/blhost FCB + secure-bootloader
        return _imx_flash(project, "bootloader", board, cfg, "flash-bootloader",   # flow, no build bin
                          output=output, sdk_home=sdk_home, dry_run=dry_run)
    if backend == "alif":                            # AE3: Alif SE tools (system package + MRAM)
        return _alif_flash(project, board, bl, output=output, dry_run=dry_run)
    f = _bootloader_bin(project, output, board)
    if backend == "dfu":
        return _bootloader_dfu(project, board, bl, f, dfu_util=dfu_util, sdk_home=sdk_home,
                               serial=serial, dry_run=dry_run)
    return _bootloader_cubeprog(project, board, bl, f, sdk_home=sdk_home, dry_run=dry_run)
