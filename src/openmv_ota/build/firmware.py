"""Build firmware per project board: invoke the openmv ``make`` build, and for an
OTA project inject a frozen ``boot.py`` via a wrapper manifest.

``build firmware`` runs the firmware's own build (``make TARGET=<board>``) in the
pegged checkout, so the result is byte-for-byte what the firmware repo produces.
For a non-OTA project that is the whole job. For an OTA project it additionally:

* freezes the OTA ``boot.py`` (``device/boot.py``) plus a generated
  ``_ota_config.py`` (trusted keys + geometry + ids), by pointing
  ``FROZEN_MANIFEST`` at a wrapper manifest that includes the board's own manifest
  and freezes both -- nothing is copied into or edited in the firmware tree; and
* drops the ECDSA verify C module (``device/ecdsa_verify.c``) into the firmware's
  ``modules/`` dir so the openmv build auto-compiles it, removing it again after.

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
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from openmv_ota.project import load_project
from openmv_ota.project.errors import ProjectError

from .errors import BuildError

MAKE = "make"

# The device sources the OTA firmware build freezes / compiles in.
_DEVICE_DIR = Path(__file__).parent / "device"
_BOOT_PY = _DEVICE_DIR / "boot.py"
_LOG_PY = _DEVICE_DIR / "log.py"          # default logger; the per-project copy overrides
_VERIFY_C = _DEVICE_DIR / "ecdsa_verify.c"
_VERIFY_MODULE = "ecdsa_verify.c"        # dropped into the firmware's modules/ dir

# The OTA installer verifies the download's TLS against a PEM CA bundle, but micropython's
# mbedtls config builds DER-only (no MBEDTLS_PEM_PARSE_C) to stay lean. Until the firmware
# enables it upstream, an OTA build transiently patches it in (restored after the build).
_MBEDTLS_CONFIG = Path("lib/micropython/extmod/mbedtls/mbedtls_config_common.h")
_PEM_ANCHOR = "#define MBEDTLS_X509_USE_C\n"
_PEM_DEFINES = "#define MBEDTLS_BASE64_C\n#define MBEDTLS_PEM_PARSE_C\n"


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
    cmod: Path | None = None
    pem: tuple[Path, str] | None = None
    try:
        build_args = ["TARGET=%s" % name, "-j%d" % (jobs or os.cpu_count() or 1)]
        if ota:
            tmp = _write_wrapper_manifest(p, repo, name)
            build_args.append("FROZEN_MANIFEST=%s" % (tmp / "manifest.py").as_posix())
            cmod = _install_verify_module(repo)
            pem = _enable_pem(repo)
        if not incremental:
            _run_make(repo, ["TARGET=%s" % name, "clean"])
        _ensure_mpy_cross(repo)
        _run_make(repo, build_args)
        outputs = _collect_outputs(repo, name, out_dir)
        return FirmwareResult(name, outputs, ota=ota,
                              build_dir=tmp if (ota and keep_build_dir) else None)
    finally:
        if pem is not None:                        # restore the mbedtls config
            pem[0].write_text(pem[1], encoding="utf-8")
        if cmod is not None:                       # restore the firmware tree
            cmod.unlink(missing_ok=True)
        if tmp is not None and not (ota and keep_build_dir):
            shutil.rmtree(tmp, ignore_errors=True)


def _enable_pem(repo: Path) -> tuple[Path, str] | None:
    """Transiently add MBEDTLS_BASE64_C + MBEDTLS_PEM_PARSE_C to the firmware's mbedtls
    config so the OTA installer can verify TLS against a PEM CA bundle. Returns
    ``(path, original_text)`` for the caller to restore, or ``None`` if it's already
    enabled (e.g. once the firmware ships it) or the config can't be patched."""
    cfg = repo / _MBEDTLS_CONFIG
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return None
    if re.search(r"(?m)^\s*#define\s+MBEDTLS_PEM_PARSE_C\b", text):
        return None                                # already enabled, nothing to do
    if _PEM_ANCHOR not in text:
        print("warning: could not enable PEM parsing in %s (unexpected mbedtls config); "
              "OTA TLS may fail to load PEM CA bundles" % cfg, file=sys.stderr)
        return None
    cfg.write_text(text.replace(_PEM_ANCHOR, _PEM_ANCHOR + _PEM_DEFINES, 1), encoding="utf-8")
    print("note: enabled MBEDTLS_PEM_PARSE_C for the OTA build (restored after); drop this "
          "once the firmware enables it upstream")
    return cfg, text


def _write_wrapper_manifest(p, repo: Path, name: str) -> Path:
    """A temp dir holding the OTA ``boot.py``, its generated ``_ota_config.py``, and
    a wrapper ``manifest.py`` that includes the board's own manifest and freezes both.
    Returns the temp dir (the caller removes it)."""
    tmp = Path(tempfile.mkdtemp(prefix="openmv-ota-fw-"))
    shutil.copy2(_BOOT_PY, tmp / "boot.py")
    (tmp / "_ota_config.py").write_text(_render_ota_config(p, name), encoding="utf-8")
    # The OTA logger, frozen as _ota_log so boot.py can use it before /rom mounts. Prefer
    # the project's editable copy (device/log.py); fall back to the bundled default.
    log_src = p.root / "device" / "log.py"
    shutil.copy2(log_src if log_src.exists() else _LOG_PY, tmp / "_ota_log.py")
    board_manifest = repo / "boards" / name / "manifest.py"
    (tmp / "manifest.py").write_text(
        'include("%s")\n' % board_manifest.as_posix()
        + 'freeze("%s", "boot.py")\n' % tmp.as_posix()
        + 'freeze("%s", "_ota_config.py")\n' % tmp.as_posix()
        + 'freeze("%s", "_ota_log.py")\n' % tmp.as_posix(),
        encoding="utf-8",
    )
    return tmp


def _render_ota_config(p, name: str) -> str:
    """Generate ``_ota_config.py`` -- the build-time constants the frozen ``boot.py``
    reads: the partition geometry, this device's ``board_id`` + the running firmware's
    platform version (both exactly as the romfs build stamps them into trailers), and
    the trusted public keys (revoked keys are dropped, so the device stops trusting
    them after this firmware update)."""
    from openmv_ota.ota import geometry
    from openmv_ota.ota.keys import read_trusted_keys
    from openmv_ota.project.config import derive_board_id
    from openmv_ota.project.project import ProjectPaths

    t = p.board(name)
    override = p.config.overrides.get(name, {})
    bid = override.get("board_id")
    board_id = int(bid) if bid is not None else derive_board_id(p.config.name, name)

    keys = "".join(
        "    0x%x: %r,\n" % (k.key_id, bytes.fromhex(k.pubkey))
        for k in read_trusted_keys(ProjectPaths(p.root).trusted_keys) if not k.revoked
    )
    return (
        "# Generated by `openmv-ota build firmware` -- do not edit.\n"
        "# Build-time constants the frozen boot.py reads.\n"
        "PARTITION_SIZE = %d\n" % t.partition_size
        + "FRONT_SIZE = %d\n" % t.front_size
        + "OTA_BLOCK = %d\n" % geometry.ota_block(t.erase_size)
        + "BOARD_ID = %d\n" % board_id
        + "PLATFORM_VERSION = %d\n" % int(p.lock.firmware.get("version_code", 0))
        + "TRUSTED_KEYS = {\n%s}\n" % keys
    )


def _install_verify_module(repo: Path) -> Path | None:
    """Drop the ECDSA verify C module into the firmware's ``modules/`` dir so the
    openmv build auto-compiles it (it globs ``modules/*.c``). Returns the installed
    path for the caller to remove after the build, or None if a file is already
    there (left intact rather than clobbered)."""
    dst = repo / "modules" / _VERIFY_MODULE
    if dst.exists():
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_VERIFY_C, dst)
    return dst


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
