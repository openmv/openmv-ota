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
# Editable device modules frozen alongside boot.py: the logger, the watchdog helper,
# and the clock (openmv_rtc, which reads BUILD_TIME out of the generated _ota_config).
# The project's device/<name> copy is preferred; the bundled build/device/<name> default
# is the fallback.
_FROZEN_DEVICE_MODULES = ("openmv_log.py", "openmv_wdt.py", "openmv_rtc.py")
_VERIFY_C = _DEVICE_DIR / "ecdsa_verify.c"
_VERIFY_MODULE = "ecdsa_verify.c"        # dropped into the firmware's modules/ dir

# The OTA installer verifies the download's TLS against a PEM CA bundle, but micropython's
# mbedtls config builds DER-only (no MBEDTLS_PEM_PARSE_C) to stay lean. Until the firmware
# enables it upstream, an OTA build points mbedtls at a patched *copy* of the per-port
# config (in a temp dir) -- the firmware source is never touched.
_MBEDTLS_COMMON = Path("lib/micropython/extmod/mbedtls/mbedtls_config_common.h")
_MBEDTLS_COMMON_INCLUDE = '#include "extmod/mbedtls/mbedtls_config_common.h"\n'
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
    for name in names:                               # a retired board crashes at boot
        from .romfs import _reject_unsupported
        _reject_unsupported(name)

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
    try:
        build_args = ["TARGET=%s" % name, "-j%d" % (jobs or os.cpu_count() or 1)]
        if ota:
            tmp = _write_wrapper_manifest(p, repo, name)
            build_args.append("FROZEN_MANIFEST=%s" % (tmp / "manifest.py").as_posix())
            cmod = _install_verify_module(repo)
            pem_arg = _pem_config_arg(repo, tmp, name)
            if pem_arg is not None:
                build_args.append(pem_arg)
        if not incremental:
            _run_make(repo, ["TARGET=%s" % name, "clean"])
        _ensure_mpy_cross(repo)
        _run_make(repo, build_args)
        outputs = _collect_outputs(repo, name, out_dir)
        outputs += _copy_wifi_blobs(repo, name, out_dir)
        return FirmwareResult(name, outputs, ota=ota,
                              build_dir=tmp if (ota and keep_build_dir) else None)
    finally:
        if cmod is not None:                       # restore the firmware tree
            cmod.unlink(missing_ok=True)
        if tmp is not None and not (ota and keep_build_dir):
            shutil.rmtree(tmp, ignore_errors=True)


def _board_port(repo: Path, board: str) -> str | None:
    """The micropython port (stm32 / alif / mimxrt) a board builds on, from its
    ``boards/<board>/board_config.mk`` (``PORT=...``)."""
    try:
        text = (repo / "boards" / board / "board_config.mk").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"(?m)^\s*PORT\s*=\s*(\w+)", text)
    return m.group(1) if m else None


def _pem_config_arg(repo: Path, tmp: Path, board: str) -> str | None:
    """A make ``MBEDTLS_CONFIG_FILE=...`` override that enables PEM parsing for the OTA
    installer's TLS, by pointing mbedtls at a **patched copy** of the board's per-port
    config (in ``tmp``) with MBEDTLS_BASE64_C + MBEDTLS_PEM_PARSE_C appended. The
    firmware source is never touched. Returns ``None`` when the firmware already enables
    PEM (then its own config is used unchanged)."""
    try:
        if re.search(r"(?m)^\s*#define\s+MBEDTLS_PEM_PARSE_C\b",
                     (repo / _MBEDTLS_COMMON).read_text(encoding="utf-8")):
            return None                            # already enabled upstream
    except OSError:
        pass                                       # can't tell -> enable it to be safe
    port = _board_port(repo, board)
    src = repo / "lib" / "micropython" / "ports" / (port or "") / "mbedtls" \
        / "mbedtls_config_port.h"
    try:
        text = src.read_text(encoding="utf-8")
    except OSError:
        print("warning: could not read the mbedtls config for %s; OTA TLS may fail to "
              "load PEM CA bundles" % board, file=sys.stderr)
        return None
    # Append the defines after the config includes the common module list (all ports do),
    # so they're inside the include guard and applied before mbedtls's check_config runs.
    patched = (text.replace(_MBEDTLS_COMMON_INCLUDE, _MBEDTLS_COMMON_INCLUDE + _PEM_DEFINES, 1)
               if _MBEDTLS_COMMON_INCLUDE in text else text + _PEM_DEFINES)
    dst = tmp / "mbedtls_config_port.h"
    dst.write_text(patched, encoding="utf-8")
    print("note: building OTA firmware with PEM parsing enabled (mbedtls config copy; "
          "source untouched); drop once the firmware enables it upstream")
    return 'MBEDTLS_CONFIG_FILE=\\"%s\\"' % dst.as_posix()


def _write_wrapper_manifest(p, repo: Path, name: str) -> Path:
    """A temp dir holding the OTA ``boot.py``, its generated ``_ota_config.py``, and
    a wrapper ``manifest.py`` that includes the board's own manifest and freezes both.
    Returns the temp dir (the caller removes it)."""
    tmp = Path(tempfile.mkdtemp(prefix="openmv-ota-fw-"))
    shutil.copy2(_BOOT_PY, tmp / "boot.py")
    (tmp / "_ota_config.py").write_text(_render_ota_config(p, name), encoding="utf-8")
    freezes = ['freeze("%s", "boot.py")\n' % tmp.as_posix(),
               'freeze("%s", "_ota_config.py")\n' % tmp.as_posix()]
    # Editable device modules (logger + watchdog) frozen so boot.py / the installer / the
    # app share them; prefer the project's copy, fall back to the bundled default.
    for mod in _FROZEN_DEVICE_MODULES:
        src = p.root / "device" / mod
        shutil.copy2(src if src.exists() else _DEVICE_DIR / mod, tmp / mod)
        freezes.append('freeze("%s", "%s")\n' % (tmp.as_posix(), mod))
    board_manifest = repo / "boards" / name / "manifest.py"
    (tmp / "manifest.py").write_text(
        'include("%s")\n' % board_manifest.as_posix() + "".join(freezes),
        encoding="utf-8")
    return tmp


def _render_ota_config(p, name: str) -> str:
    """Generate ``_ota_config.py`` -- the build-time constants the frozen ``boot.py``
    reads: the partition geometry, this device's ``product_id`` + the running firmware's
    platform version (both exactly as the romfs build stamps them into trailers), and
    the trusted public keys (revoked keys are dropped, so the device stops trusting
    them after this firmware update)."""
    from openmv_ota.ota import geometry
    from openmv_ota.ota.keys import read_trusted_keys
    from openmv_ota.project.config import derive_product_id
    from openmv_ota.project.project import ProjectPaths

    t = p.board(name)
    override = p.config.overrides.get(name, {})
    bid = override.get("product_id")
    product_id = int(bid) if bid is not None else derive_product_id(p.config.name, name)

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
        + "PRODUCT_ID = %d\n" % product_id
        + "ACCOUNT_ID = %r\n" % p.config.account_id
        + "PLATFORM_VERSION = %d\n" % int(p.lock.firmware.get("version_code", 0))
        + "BUILD_TIME = %d\n" % _build_time(p)
        + "TRUSTED_KEYS = {\n%s}\n" % keys
    )


def _build_time(p) -> int:
    """The clock floor for ``openmv_rtc``: Unix seconds for the lock's
    ``generated_at``. A device's clock cannot legitimately read earlier than
    this, which is how the firmware detects a dead or unset RTC without a
    network round trip.

    Taken from the LOCK rather than the wall clock at build time, so a build
    stays reproducible: the same lock yields the same firmware. 0 if the lock
    predates the field or cannot be parsed -- openmv_rtc then reports the clock
    untrusted rather than trusting a floor it doesn't have."""
    stamp = getattr(p.lock, "generated_at", "") or ""
    try:
        from datetime import datetime, timezone
        return int(datetime.fromisoformat(
            stamp.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return 0


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
    ``firmware_M55_HE.bin``. The bootloader-combined ``openmv.bin`` and the unpadded
    ``firmware.toc`` are deliberately not collected (the AE3's padded ``firmware_pad.toc``,
    written alongside its bootloader, is)."""
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
    boot = bdir / "bootloader.bin"
    if boot.exists():                              # the OpenMV bootloader (STM32/N6 ports);
        collected.append(_copy(boot, out_dir / ("%s-bootloader.bin" % name)))   # `flash bootloader`
    toc = bdir / "firmware_pad.toc"
    if toc.exists():                               # AE3: the padded TOC written with the SBL
        collected.append(_copy(toc, out_dir / ("%s-firmware_pad.toc" % name)))  # bootloader
    return collected


def _copy_wifi_blobs(repo: Path, name: str, out_dir: Path) -> list[Path]:
    """Copy a board's flash-time wifi/bt blobs (the Arduino CYW4343 firmware) out of the
    firmware tree alongside its image, so the output dir is a self-contained, version-matched
    flashable set -- the blob is pinned to the exact firmware just built, not whatever the
    checkout holds at flash time. A no-op for boards that don't bundle any."""
    from openmv_ota.romfs.boards import load_boards

    board = load_boards().get(name)
    flash = board.flash if board else None
    if not flash or not flash.get("wifi"):
        return []
    src_dir = repo / flash["wifi_dir"]
    copied = []
    for w in flash["wifi"]:
        src = src_dir / w["file"]
        if not src.exists():
            raise BuildError("wifi blob %s not found in the firmware tree (expected at %s)"
                             % (w["file"], src), exit_code=1)
        copied.append(_copy(src, out_dir / w["file"]))
    return copied


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
