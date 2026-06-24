"""Build firmware per project board: invoke the openmv ``make`` build, and for an
OTA project inject a frozen ``boot.py`` via a wrapper manifest.

``build firmware`` runs the firmware's own build (``make TARGET=<board>``) in the
pegged checkout, so the result is byte-for-byte what the firmware repo produces.
For a non-OTA project that is the whole job. For an OTA project it additionally
freezes an OTA ``boot.py`` into the image by pointing ``FROZEN_MANIFEST`` at a
generated wrapper manifest that includes the board's own manifest and adds the
boot script -- no files are copied into or edited in the firmware tree.

The build is **clean by default**: a stale ``build/<board>`` tree fails at link
with a misleading ``__cyg_profile_func_enter`` error (imlib is compiled with
``-finstrument-functions``), unrelated to anything we inject. ``--incremental``
skips the clean for fast iteration when the tree is known good.

Before the board build we build the host ``mpy-cross`` on its own (see
``_ensure_mpy_cross``) when the tree hasn't already: the board build would build
it as a side effect, but the openmv Makefile exports the board's ARM ``CFLAGS``,
which leak into that host sub-build and break a from-scratch ``mpy-cross`` -- so a
clean checkout could never build firmware without this step.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from openmv_ota.project import load_project
from openmv_ota.project.errors import ProjectError

from .errors import BuildError

MAKE = "make"

# Placeholder frozen boot script. The real trailer-parse + signature/SHA verify +
# status state machine + FRONT/BACK slot selection lands in a later step; this just
# proves the injection path and runs after the stock frozen ``_boot.py``.
_BOOT_PLACEHOLDER = (
    "# boot.py - frozen OTA boot hook (placeholder).\n"
    "# Runs after the board's stock _boot.py. The real OTA slot-selection +\n"
    "# signature verification logic is injected here in a later step.\n"
)


@dataclass
class FirmwareResult:
    board: str
    outputs: list[Path] = field(default_factory=list)  # collected firmware image(s)
    ota: bool = False                                   # boot.py was injected
    build_dir: Path | None = None                       # wrapper temp dir, when kept


def build_firmware(
    project: str | Path,
    *,
    output: str | Path | None = None,
    boards: list[str] | None = None,
    firmware: str | Path | None = None,
    jobs: int | None = None,
    incremental: bool = False,
    keep_build_dir: bool = False,
) -> list[FirmwareResult]:
    project = Path(project)
    try:
        p = load_project(project, firmware=firmware)  # verify=True: refuses on drift
    except ProjectError as e:
        raise BuildError(str(e), exit_code=e.exit_code) from None

    out_dir = Path(output) if output else project / "build"
    names = _select_boards(p.targets, boards)
    if not names:
        raise BuildError("no matching boards in this project")

    repo = p.firmware_path
    out_dir.mkdir(parents=True, exist_ok=True)
    return [
        _build_one(p, repo, name, out_dir, jobs=jobs, incremental=incremental,
                   keep_build_dir=keep_build_dir)
        for name in names
    ]


def _select_boards(targets, boards: list[str] | None) -> list[str]:
    """Firmware is per-board (not per-partition): the unique board names, sorted,
    filtered to ``boards`` when given."""
    names: list[str] = []
    for t in targets:
        if t.name not in names and (not boards or t.name in boards):
            names.append(t.name)
    return sorted(names)


def _build_one(p, repo: Path, name: str, out_dir: Path, *, jobs, incremental,
               keep_build_dir) -> FirmwareResult:
    ota = p.config.ota
    tmp: Path | None = None
    try:
        build_args = ["TARGET=%s" % name, "-j%d" % (jobs or os.cpu_count() or 1)]
        if ota:
            tmp = _write_wrapper_manifest(repo, name)
            build_args.append("FROZEN_MANIFEST=%s" % (tmp / "manifest.py").as_posix())
        if not incremental:
            _run_make(repo, ["TARGET=%s" % name, "clean"])
        _ensure_mpy_cross(repo)
        _run_make(repo, build_args)
        outputs = _collect_outputs(repo, name, out_dir)
        return FirmwareResult(name, outputs, ota=ota,
                              build_dir=tmp if (ota and keep_build_dir) else None)
    finally:
        if tmp is not None and not (ota and keep_build_dir):
            shutil.rmtree(tmp, ignore_errors=True)


def _write_wrapper_manifest(repo: Path, name: str) -> Path:
    """A temp dir holding the OTA ``boot.py`` and a wrapper ``manifest.py`` that
    includes the board's own manifest and freezes the boot script. Returns the
    temp dir (the caller removes it)."""
    tmp = Path(tempfile.mkdtemp(prefix="openmv-ota-fw-"))
    (tmp / "boot.py").write_text(_BOOT_PLACEHOLDER, encoding="utf-8")
    board_manifest = repo / "boards" / name / "manifest.py"
    (tmp / "manifest.py").write_text(
        'include("%s")\n' % board_manifest.as_posix()
        + 'freeze("%s", "boot.py")\n' % tmp.as_posix(),
        encoding="utf-8",
    )
    return tmp


def _collect_outputs(repo: Path, name: str, out_dir: Path) -> list[Path]:
    """Copy the firmware image(s) the build produced into ``out_dir``. Both ports
    name their images ``firmware*.bin`` in ``build/<board>/bin``: stm32 emits a
    single ``firmware.bin``; Alif emits a per-core ``firmware_M55_HP.bin`` /
    ``firmware_M55_HE.bin``. The bootloader-combined ``openmv.bin`` and the
    bootloader-written ``firmware.toc`` are deliberately not collected."""
    bdir = repo / "build" / name / "bin"
    collected: list[Path] = []
    for src in sorted(bdir.glob("firmware*.bin")):
        # firmware.bin -> <board>-firmware.bin;
        # firmware_M55_HP.bin -> <board>-firmware-M55_HP.bin.
        suffix = src.stem[len("firmware"):].lstrip("_")
        dst_name = "%s-firmware-%s.bin" % (name, suffix) if suffix \
            else "%s-firmware.bin" % name
        collected.append(_copy(src, out_dir / dst_name))
    if not collected:
        raise BuildError("firmware build produced no image for %s (looked for "
                         "firmware*.bin in %s)" % (name, bdir), exit_code=1)
    return collected


def _copy(src: Path, dst: Path) -> Path:
    shutil.copy2(src, dst)
    return dst


def _ensure_mpy_cross(repo: Path) -> None:
    """Build the host ``mpy-cross`` on its own if the firmware tree hasn't yet.

    ``make TARGET=<board>`` builds ``mpy-cross`` as a side effect, but the openmv
    Makefile ``export``s the board's ARM ``CFLAGS``, which leak into that host
    sub-build and make a *from-scratch* ``mpy-cross`` fail to compile -- the host
    compiler rejects ``-mcpu=cortex-m7`` and friends. Building it here in its own
    ``make`` invocation, where no board CFLAGS are in scope, sidesteps the leak; the
    board build then reuses the binary instead of rebuilding it. A tree that already
    has ``mpy-cross`` built (or that isn't micropython-based) is left untouched.
    """
    mpy_dir = repo / "lib" / "micropython" / "mpy-cross"
    if not mpy_dir.is_dir() or (mpy_dir / "build" / "mpy-cross").exists():
        return
    # Strip compiler-flag vars so nothing in our environment leaks into this host
    # build either (the board-CFLAGS leak above is the openmv Makefile's doing, but
    # an inherited CFLAGS would break it just the same).
    env = {k: v for k, v in os.environ.items()
           if k not in ("CFLAGS", "CXXFLAGS", "CPPFLAGS")}
    try:
        subprocess.run([MAKE, "-C", str(mpy_dir)], check=True, env=env)
    except FileNotFoundError:
        raise BuildError("make not found - a firmware build toolchain is required",
                         exit_code=1) from None
    except subprocess.CalledProcessError as e:
        raise BuildError("mpy-cross build failed (make -C %s): exit %d"
                         % (mpy_dir, e.returncode), exit_code=1) from None


def _run_make(repo: Path, args: list[str]) -> None:
    try:
        subprocess.run([MAKE, *args], cwd=str(repo), check=True)
    except FileNotFoundError:
        raise BuildError("make not found - a firmware build toolchain is required",
                         exit_code=1) from None
    except subprocess.CalledProcessError as e:
        raise BuildError("firmware build failed (make %s): exit %d"
                         % (" ".join(args), e.returncode), exit_code=1) from None
