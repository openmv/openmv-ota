"""Build a ROMFS image per project target: compile, convert, pack, (OTA) sign."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from openmv_ota.ota import geometry
from openmv_ota.project import load_project
from openmv_ota.project.config import derive_board_id
from openmv_ota.project.errors import ProjectError
from openmv_ota.project.project import COPROCESSOR_APP, ProjectPaths
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


def _load_signer(p, app_dir: Path, key_id: int, *, require_role: str) -> _OtaSigner:
    """Resolve a signing context: the app version + rollback floor from
    ``app/settings.json``, plus the trusted key ``key_id`` (which must have role
    ``require_role`` and not be revoked) and its private PEM. Shared by ``build
    romfs`` (the OTA signing key) and ``build factory-romfs`` (a factory key)."""
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

    entry = next((k for k in trusted if k.key_id == key_id), None)
    if entry is None:
        raise BuildError("key 0x%04x is not in keys/trusted_keys.json" % key_id, exit_code=1)
    if entry.role != require_role:
        raise BuildError("key 0x%04x is a %s key, not a %s key"
                         % (key_id, entry.role, require_role), exit_code=1)
    if entry.revoked:
        hint = ("; run `openmv-ota project keys rotate` to move to the next key"
                if require_role == "ota" else "")
        raise BuildError("%s key 0x%04x is revoked%s" % (require_role, key_id, hint), exit_code=1)
    pem_path = ProjectPaths(p.root).private_keys_dir / ("%s-%04x.pem" % (entry.role, key_id))
    try:
        private_key = load_private_key_pem(pem_path.read_bytes())
    except OSError:
        raise BuildError(
            "private key %s not found - only the signing machine has it; build the body "
            "without signing elsewhere, or provision the key here" % pem_path,
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


def _build_trailer(signer: _OtaSigner, p, body: bytes, system_info: dict, pad_size: int) -> bytes:
    """Build + sign the complete trailer for ``body`` and return its packed bytes
    (``header || meta || signature || crc32``). Every field is final and calculated —
    notably ``pad_size`` (the 0xFF gap to this slot's status sector, signed) and the
    crc32 — so the trailer is a self-contained, verifiable artifact. The device writes
    these bytes to the slot's last erase block; the 0xFF that fills the rest of that
    block comes from the erase, not from this file."""
    from openmv_ota.ota import Trailer, pack_trailer, signed_region
    from openmv_ota.ota.sign import sign_region

    trailer = Trailer(
        body_size=len(body),
        pad_size=pad_size,
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
    return pack_trailer(trailer)


def _capacity(project, target) -> tuple[int, str]:
    """The usable image budget for a target and the name of what bounds it. A *main*
    OTA partition gets a slot (half the partition) less its status + trailer sectors;
    a coprocessor partition is always a plain romfs that fills the whole partition."""
    if project.config.ota and target.role == "main":
        return target.front_size - geometry.slot_overhead(target.erase_size), "OTA slot"
    return target.partition_size, "ROMFS partition"


@dataclass
class BuildResult:
    target: str
    partition_index: int
    output: Path                   # <board>-romfs.img (non-OTA) or <board>-romfs.zip bundle (OTA)
    size: int
    capacity: int
    bound: str = "ROMFS partition"  # what capacity measures (partition, or OTA slot)
    ota: bool = False              # output is a signed .zip bundle
    build_dir: Path | None = None  # set when --keep-build-dir


def build_romfs(
    project: str | Path,
    *,
    app: str | Path | None = None,
    output: str | Path | None = None,
    boards: list[str] | None = None,
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
    copro_dir = project / COPROCESSOR_APP
    out_dir = Path(output) if output else project / "build"
    _warn_board_id_collisions(p.config)

    targets = _select_targets(p.targets, boards)
    if not targets:
        raise BuildError("no matching targets in this project")

    mpy_cmd = mpy.resolve_mpy_cross(p) if compile_py else None
    ota_signer = (_load_signer(p, app_dir, p.config.signing_key_id, require_role="ota")
                  if p.config.ota else None)
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

    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for t in targets:
        if t.role == "main":
            results.append(_build_one(
                p, t, app_dir, out_dir, ctx, mpy_cmd, ota_signer, app_version, vendor,
                convert_models=convert_models, mpy_extra=list(mpy_extra or []),
                allow_oversize=allow_oversize, keep_build_dir=keep_build_dir))
        else:  # coprocessor: always a plain romfs, built from app-coprocessor/
            results.append(_coprocessor_one(
                p, t, copro_dir, out_dir, ctx, mpy_cmd,
                convert_models=convert_models, mpy_extra=list(mpy_extra or []),
                allow_oversize=allow_oversize, keep_build_dir=keep_build_dir))
    return results


def _select_targets(targets, boards):
    sel = list(targets)
    if boards:
        sel = [t for t in sel if t.name in boards]
    return sel


def _target_name(t) -> str:
    """Output basename. The main partition keeps the bare board name; a coprocessor
    partition is suffixed with its role (e.g. ``OPENMV_AE3-coprocessor``)."""
    return t.name if t.role == "main" else "%s-%s" % (t.name, t.role)


def _warn_unset_board_id(t, system_info: dict) -> None:
    if system_info["board_id"] == 0:
        print("warning: %s has board_id 0 (unset); the cross-flash guard is off - set "
              "board_id under [targets.%s] in openmv-ota.toml" % (t.name, t.name),
              file=sys.stderr)


def _build_body(p, t, app_dir, ctx, mpy_cmd, app_version, vendor, *, convert_models, mpy_extra):
    """Stage + compile + convert + system.json + pack. Returns ``(body, system_info,
    tmp_dir)``; the caller is responsible for removing ``tmp_dir``."""
    tmp = Path(tempfile.mkdtemp(prefix="openmv-ota-build-"))
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

    # The read-only system.json (board identity + provenance) goes into the staged
    # app, so every image - OTA or not - carries it at /rom/system.json.
    system_info = _build_system_info(p, t, app_version, vendor)
    (stage / "system.json").write_text(
        json.dumps(system_info, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    body = build_image(str(stage), t.alignment_rules)
    return body, system_info, tmp


def _coprocessor_one(p, t, copro_dir, out_dir, ctx, mpy_cmd, *,
                     convert_models, mpy_extra, allow_oversize, keep_build_dir) -> BuildResult:
    """A coprocessor partition is never OTA: build a plain romfs that fills the whole
    partition, from ``app-coprocessor/`` (its own version/vendor, no signing). This is
    the image the main core writes into the helper core's slot."""
    settings = _read_settings(copro_dir)
    return _build_one(
        p, t, copro_dir, out_dir, ctx, mpy_cmd, None,
        settings.get("app_version"), str(settings.get("vendor", "")),
        convert_models=convert_models, mpy_extra=mpy_extra,
        allow_oversize=allow_oversize, keep_build_dir=keep_build_dir)


def _build_one(p, t, app_dir, out_dir, ctx, mpy_cmd, ota_signer, app_version, vendor, *,
               convert_models, mpy_extra, allow_oversize, keep_build_dir) -> BuildResult:
    body, system_info, tmp = _build_body(p, t, app_dir, ctx, mpy_cmd, app_version, vendor,
                                         convert_models=convert_models, mpy_extra=mpy_extra)
    try:
        capacity, bound = _capacity(p, t)
        if len(body) > capacity and not allow_oversize:
            raise BuildError(
                "%s image is %d bytes but the %s holds %d (%d over); pass --allow-oversize"
                % (t.name, len(body), bound, capacity, len(body) - capacity), exit_code=1)

        name = _target_name(t)
        if ota_signer is not None:
            from openmv_ota.ota import bundle
            _warn_unset_board_id(t, system_info)
            pad_size = max(0, capacity - len(body))  # 0xFF gap to the FRONT status sector
            trailer_bytes = _build_trailer(ota_signer, p, body, system_info, pad_size)
            out_path = out_dir / (name + "-romfs.zip")  # body + trailer, one file
            bundle.write_bundle(out_path, body, trailer_bytes)
        else:
            out_path = out_dir / (name + "-romfs.img")  # just the ROMFS body
            out_path.write_bytes(body)

        return BuildResult(t.name, t.partition_index, out_path, len(body), capacity,
                           bound=bound, ota=ota_signer is not None,
                           build_dir=tmp if keep_build_dir else None)
    finally:
        if not keep_build_dir:
            shutil.rmtree(tmp, ignore_errors=True)


# --- factory image (dual-slot golden + initial FRONT) -----------------------

def build_factory_romfs(
    project: str | Path,
    *,
    app: str | Path | None = None,
    output: str | Path | None = None,
    boards: list[str] | None = None,
    compile_py: bool = True,
    convert_models: bool = True,
    mpy_extra: list[str] | None = None,
    vela_extra: list[str] | None = None,
    stedgeai_extra: list[str] | None = None,
    vela_optimise: str = "Performance",
    stedgeai_optimization: int = 3,
    firmware: str | Path | None = None,
    factory_key: int | None = None,
    keep_build_dir: bool = False,
) -> list[BuildResult]:
    """Compose the factory ROMFS partition image per target: golden BACK + initial
    FRONT, both factory-signed, with status sectors and padding. One
    ``<board>-factory-romfs.img`` per main partition. Coprocessor partitions have no
    golden/trial concept, so they get the same plain ``<board>-coprocessor-romfs.img``
    as a regular build -- the image the main core writes into the helper slot."""
    from openmv_ota.ota.keys import FACTORY_KEY_ID_BASE

    project = Path(project)
    try:
        p = load_project(project, firmware=firmware)
    except ProjectError as e:
        raise BuildError(str(e), exit_code=e.exit_code) from None
    if not p.config.ota:
        raise BuildError("factory-romfs needs an OTA project (create with "
                         "`openmv-ota project new --ota`)", exit_code=1)

    app_dir = Path(app) if app else project / "app"
    copro_dir = project / COPROCESSOR_APP
    out_dir = Path(output) if output else project / "build"
    _warn_board_id_collisions(p.config)

    targets = _select_targets(p.targets, boards)
    if not targets:
        raise BuildError("no matching targets in this project")

    mpy_cmd = mpy.resolve_mpy_cross(p) if compile_py else None
    key_id = factory_key if factory_key is not None else FACTORY_KEY_ID_BASE
    signer = _load_signer(p, app_dir, key_id, require_role="factory")
    ctx = ModelContext(
        sdk_home=p.sdk_home, vela_path=p.vela_path, stedgeai_path=p.stedgeai_path,
        vela_optimise=vela_optimise, stedgeai_optimization=stedgeai_optimization,
        vela_extra=list(vela_extra or []), stedgeai_extra=list(stedgeai_extra or []),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for t in targets:
        if t.role == "main":
            results.append(_factory_one(
                p, t, app_dir, out_dir, ctx, mpy_cmd, signer,
                signer.app_version, signer.vendor, convert_models=convert_models,
                mpy_extra=list(mpy_extra or []), keep_build_dir=keep_build_dir))
        else:  # coprocessor: a plain romfs, same as a regular build
            results.append(_coprocessor_one(
                p, t, copro_dir, out_dir, ctx, mpy_cmd,
                convert_models=convert_models, mpy_extra=list(mpy_extra or []),
                allow_oversize=False, keep_build_dir=keep_build_dir))
    return results


def _compose_slot(body: bytes, pad: int, status_sector: bytes, trailer_bytes: bytes,
                  block: int, slot_size: int) -> bytes:
    """One slot: ``body || 0xFF pad || status block || trailer block`` == slot_size."""
    trailer_block = trailer_bytes + b"\xff" * (block - len(trailer_bytes))
    slot = body + b"\xff" * pad + status_sector + trailer_block
    assert len(slot) == slot_size, (len(slot), slot_size)
    return slot


def _factory_one(p, t, app_dir, out_dir, ctx, mpy_cmd, signer, app_version, vendor, *,
                 convert_models, mpy_extra, keep_build_dir) -> BuildResult:
    from openmv_ota.ota import status

    body, system_info, tmp = _build_body(p, t, app_dir, ctx, mpy_cmd, app_version, vendor,
                                         convert_models=convert_models, mpy_extra=mpy_extra)
    try:
        block = geometry.ota_block(t.erase_size)
        front_size = t.front_size
        back_size = t.partition_size - front_size
        front_cap = front_size - 2 * block
        back_cap = back_size - 2 * block
        if len(body) > front_cap:  # the smaller slot bounds it
            raise BuildError(
                "%s image is %d bytes but a factory slot holds %d (%d over)"
                % (t.name, len(body), front_cap, len(body) - front_cap), exit_code=1)
        _warn_unset_board_id(t, system_info)

        # FRONT: mountable at first boot (post-OTA-confirmed shape).
        front_pad = front_cap - len(body)
        front = _compose_slot(
            body, front_pad,
            status.build_status_sector(block, pending=True, tried=True, confirmed=True),
            _build_trailer(signer, p, body, system_info, front_pad), block, front_size)
        # BACK: golden / factory state (confirmed only), never trialed.
        back_pad = back_cap - len(body)
        back = _compose_slot(
            body, back_pad,
            status.build_status_sector(block, pending=False, tried=False, confirmed=True),
            _build_trailer(signer, p, body, system_info, back_pad), block, back_size)

        name = _target_name(t)
        out_path = out_dir / (name + "-factory-romfs.img")
        out_path.write_bytes(front + back)
        return BuildResult(t.name, t.partition_index, out_path, len(body), front_cap,
                           bound="factory slot", ota=True,
                           build_dir=tmp if keep_build_dir else None)
    finally:
        if not keep_build_dir:
            shutil.rmtree(tmp, ignore_errors=True)
