"""Build a ROMFS image per project target: compile, convert, pack, (OTA) sign."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from openmv_ota.ota import geometry
from openmv_ota.project import load_project
from openmv_ota.project.config import derive_board_id
from openmv_ota.project.errors import ProjectError
from openmv_ota.project.project import ProjectPaths
from openmv_ota.romfs.builder import build_image

from .compile import mpy
from .compile.models import MODEL_SUFFIXES, ModelContext, convert_model
from .errors import BuildError
from .staging import iter_files, stage_app


@dataclass
class _OtaSigner:
    """The project-wide signing context for OTA builds (resolved once)."""

    app_version: str
    payload_version: int
    payload_version_floor: int
    vendor: str
    key_id: int
    sig_alg: int        # COSE id
    alg: object         # AlgSpec
    private_key: object  # loaded private key


def _load_ota_signer(p, app_dir: Path) -> _OtaSigner:
    """Resolve the OTA signing context: the app version from ``app/settings.json``
    and the project's current signing key + its private PEM."""
    from openmv_ota.ota import algorithm_for, read_trusted_keys
    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.keys import load_private_key_pem
    from openmv_ota.ota.version import encode_app_version

    settings_path = app_dir / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise BuildError("OTA build needs a readable %s: %s" % (settings_path, e),
                         exit_code=1) from None
    app_version = settings.get("app_version")
    if not app_version:
        raise BuildError("%s is missing app_version" % settings_path, exit_code=1)
    try:
        payload_version = encode_app_version(app_version)
        trusted = read_trusted_keys(ProjectPaths(p.root).trusted_keys)
    except OtaError as e:
        raise BuildError(str(e), exit_code=1) from None

    # rollback_floor: an anti-rollback floor (the oldest version ever allowed back).
    # Optional; defaults to 0 (no floor). Must not exceed the image's own version.
    floor_str = settings.get("rollback_floor")
    payload_version_floor = 0
    if floor_str:
        try:
            payload_version_floor = encode_app_version(floor_str)
        except OtaError as e:
            raise BuildError("invalid rollback_floor: %s" % e, exit_code=1) from None
        if payload_version_floor > payload_version:
            raise BuildError(
                "rollback_floor %s can't exceed app_version %s in %s"
                % (floor_str, app_version, settings_path), exit_code=1)

    key_id = p.config.signing_key_id
    entry = next((k for k in trusted if k.key_id == key_id), None)
    if entry is None:
        raise BuildError("signing key %s is not in keys/trusted_keys.json" % key_id, exit_code=1)
    pem_path = ProjectPaths(p.root).private_keys_dir / ("ota-%04x.pem" % key_id)
    try:
        private_key = load_private_key_pem(pem_path.read_bytes())
    except OSError:
        raise BuildError(
            "private signing key %s not found - only the signing machine has it; "
            "build the body without signing elsewhere, or provision the key here" % pem_path,
            exit_code=1) from None
    return _OtaSigner(app_version, payload_version, payload_version_floor,
                      str(settings.get("vendor", "")), key_id, entry.alg,
                      algorithm_for(entry.alg), private_key)


def _warn_board_id_collisions(config) -> None:
    """Warn if two different boards were given the same board_id - the cross-flash
    guard can't tell them apart. Auto-assigned ids are distinct, so this only fires
    on a manual edit. (board_id 0 is "unset" and skipped.)"""
    seen: dict[int, str] = {}
    for name, ov in config.overrides.items():
        bid = int(ov.get("board_id", 0))
        if bid == 0:
            continue
        if bid in seen:
            print("warning: board_id %d is shared by %s and %s; the cross-flash guard "
                  "can't tell them apart - give each board a distinct board_id"
                  % (bid, seen[bid], name), file=sys.stderr)
        else:
            seen[bid] = name


def _read_settings(app_dir: Path) -> dict:
    """Best-effort read of ``app/settings.json`` (app_version, vendor). Returns ``{}``
    when absent or unreadable - a non-OTA build doesn't require it; the OTA path
    validates it strictly via :func:`_load_ota_signer`."""
    try:
        return json.loads((app_dir / "settings.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _build_system_info(p, t, app_version, vendor: str) -> dict:
    """The derived system/identity info for a target. Written into the ROMFS as
    ``system.json`` (read by the app, OTA or not) and mirrored verbatim into the OTA
    trailer's metadata (so host tools can read it without a ROMFS reader). Composed
    from the lock (provenance) + config (per-board identity); never user-edited.
    ``board_id`` comes from the config when pinned (OTA projects pin it explicitly,
    so it's frozen for the cross-flash guard) and is otherwise auto-derived so even
    a non-OTA image carries a stable product id. ``board_name`` defaults to the
    product name."""
    override = p.config.overrides.get(t.name, {})
    product = p.config.name
    bid = override.get("board_id")
    board_id = int(bid) if bid is not None else derive_board_id(product, t.name)
    return {
        "product": product,
        "board": t.name,
        "board_id": board_id,
        "board_name": str(override.get("board_name") or product),
        "app_version": app_version,
        "vendor": vendor,
        "ota": p.config.ota,
        "firmware": {
            "version": p.lock.firmware.get("version"),
            "commit": p.lock.firmware.get("commit"),
        },
        "micropython": p.lock.micropython.get("version"),
        "toolchain": {
            "mpy_cross": p.lock.toolchain.get("mpy_cross", {}).get("version"),
            "vela": p.lock.toolchain.get("vela", {}).get("version"),
            "stedgeai": p.lock.toolchain.get("stedgeai", {}).get("version"),
            "sdk": p.lock.sdk.get("version"),
        },
    }


def _attach_trailer(signer: _OtaSigner, p, body: bytes, system_info: dict, block: int) -> bytes:
    """Stamp + sign a trailer for ``body`` and return ``body || trailer-sector``. The
    trailer's metadata is the same ``system_info`` dict packed into the ROMFS, and the
    sector is padded with 0xFF to one flash erase ``block``."""
    from openmv_ota.ota import Trailer, pack_trailer, signed_region
    from openmv_ota.ota.sign import sign_region

    trailer = Trailer(
        body_size=len(body),
        pad_size=0,  # the real slot padding is set later by slot composition
        meta=system_info,
        board_id=int(system_info["board_id"]),
        min_platform_version=int(p.lock.firmware.get("version_code", 0)),
        payload_version=signer.payload_version,
        payload_version_floor=signer.payload_version_floor,
        key_id=signer.key_id,
        sig_alg=signer.sig_alg,
        body_sha256=hashlib.sha256(body).digest(),
    )
    trailer.signature = sign_region(signer.private_key, signed_region(trailer), signer.alg)
    trailer_bytes = pack_trailer(trailer)
    sector = trailer_bytes + b"\xff" * (block - len(trailer_bytes))
    return body + sector


def _capacity(project, target) -> tuple[int, str]:
    """The usable image budget for a target and the name of what bounds it. An OTA
    image gets a slot (half the partition) less its status + trailer sectors."""
    if project.config.ota:
        return target.front_size - geometry.slot_overhead(target.erase_size), "OTA slot"
    return target.partition_size, "ROMFS partition"


@dataclass
class BuildResult:
    target: str
    partition_index: int
    output: Path
    size: int
    capacity: int
    bound: str = "ROMFS partition"  # what capacity measures (partition, or OTA slot)
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
    _warn_board_id_collisions(p.config)

    targets = _select_targets(p.targets, boards, partition)
    if not targets:
        raise BuildError("no matching targets in this project")

    mpy_cmd = mpy.resolve_mpy_cross(p) if compile_py else None
    ota_signer = _load_ota_signer(p, app_dir) if p.config.ota else None
    if ota_signer is not None:
        app_version, vendor = ota_signer.app_version, ota_signer.vendor
    else:
        settings = _read_settings(app_dir)
        app_version, vendor = settings.get("app_version"), str(settings.get("vendor", ""))

    ctx = ModelContext(
        sdk_home=p.sdk_home, vela_path=p.vela_path, stedgeai_path=p.stedgeai_path,
        vela_optimise=vela_optimise, stedgeai_optimization=stedgeai_optimization,
        vela_extra=list(vela_extra or []), stedgeai_extra=list(stedgeai_extra or []),
    )

    multi = {name for name, c in Counter(t.name for t in targets).items() if c > 1}
    out_dir.mkdir(parents=True, exist_ok=True)
    return [
        _build_one(p, t, app_dir, out_dir, ctx, multi, mpy_cmd, ota_signer, app_version, vendor,
                   convert_models=convert_models,
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


def _build_one(p, t, app_dir, out_dir, ctx, multi, mpy_cmd, ota_signer, app_version, vendor, *,
               convert_models, mpy_extra, allow_oversize, keep_build_dir) -> BuildResult:
    tmp = Path(tempfile.mkdtemp(prefix="openmv-ota-build-"))
    try:
        stage = stage_app(app_dir, tmp / "app")

        if mpy_cmd is not None:
            for src in iter_files(stage, (".py",)):
                mpy.compile_py(mpy_cmd, t.mpy_args + mpy_extra, src, src.with_suffix(".mpy"))
                src.unlink()

        if convert_models and t.npu:
            for model in iter_files(stage, MODEL_SUFFIXES):
                data = convert_model(t, ctx, model)
                if data is not None:
                    model.write_bytes(data)

        # Generate the read-only system.json (board identity + provenance) into the
        # staged app, so every image - OTA or not - carries it at /rom/system.json.
        system_info = _build_system_info(p, t, app_version, vendor)
        (stage / "system.json").write_text(
            json.dumps(system_info, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        body = build_image(str(stage), t.alignment_rules)
        capacity, bound = _capacity(p, t)
        if len(body) > capacity and not allow_oversize:
            raise BuildError(
                "%s image is %d bytes but the %s holds %d (%d over); "
                "pass --allow-oversize"
                % (t.name, len(body), bound, capacity, len(body) - capacity),
                exit_code=1,
            )

        body_size = len(body)
        if ota_signer is not None:
            if system_info["board_id"] == 0:
                print("warning: %s has board_id 0 (unset); the cross-flash guard is off - "
                      "set board_id under [targets.%s] in openmv-ota.toml"
                      % (t.name, t.name), file=sys.stderr)
            image = _attach_trailer(ota_signer, p, body, system_info,
                                    geometry.ota_block(t.erase_size))
        else:
            image = body
        name = "%s-p%d" % (t.name, t.partition_index) if t.name in multi else t.name
        out_path = out_dir / (name + ".romfs")
        out_path.write_bytes(image)
        return BuildResult(t.name, t.partition_index, out_path, body_size, capacity,
                           bound=bound, build_dir=tmp if keep_build_dir else None)
    finally:
        if not keep_build_dir:
            shutil.rmtree(tmp, ignore_errors=True)
