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
from openmv_ota.project.config import derive_product_id
from openmv_ota.project.errors import ProjectError
from openmv_ota.project.project import COPROCESSOR_APP, ProjectPaths
from openmv_ota.romfs import boards as boards_mod
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
    backend: object     # a Signer (encrypted PEM / PKCS#11 / KMS / custom)


def _load_signer(p, app_dir: Path, key_id: int, *, require_role: str,
                 key_passphrase_file=None, allow_dev_key: bool = False) -> _OtaSigner:
    """Resolve a signing context: the app version + rollback floor from
    ``app/settings.json``, plus the trusted key ``key_id`` (which must have role
    ``require_role`` and not be revoked) and a ``Signer`` for it. Shared by ``build
    romfs`` (the OTA signing key) and ``build factory-romfs`` (a factory key)."""
    from openmv_ota.ota import algorithm_for, read_trusted_keys
    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.signer import build_signer
    from openmv_ota.ota.version import encode_app_version
    from openmv_ota.project.passphrase import resolve_passphrase

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
    alg = algorithm_for(entry.alg)
    provider = lambda: resolve_passphrase(p.root, passphrase_file=key_passphrase_file)  # noqa: E731
    try:
        backend = build_signer(entry, alg, private_keys_dir=ProjectPaths(p.root).private_keys_dir,
                               passphrase_provider=provider)
    except (OtaError, ProjectError) as e:
        raise BuildError(str(e), exit_code=1) from None
    # the signer's key must be the one the devices trust -- catches a wrong/stale PEM, a swapped
    # token, or a mis-mapped KMS key before it signs a release nothing can install.
    if backend.public_point_hex() != entry.pubkey:
        raise BuildError("key 0x%04x: the signer's public key does not match "
                         "keys/trusted_keys.json (wrong key material?)" % key_id, exit_code=1)
    if backend.is_dev_key and not allow_dev_key:
        raise BuildError(
            "refusing to sign a production image with a dev signing key (0x%04x); use a real "
            "encrypted key, or pass --allow-dev-key for a throwaway build" % key_id, exit_code=1)
    return _OtaSigner(app_version, payload_version, payload_version_floor,
                      str(settings.get("vendor", "")), key_id, entry.alg, alg, backend)


def _warn_product_id_collisions(config) -> None:
    """Warn if two different boards were given the same product_id - the cross-flash
    guard can't tell them apart. Auto-assigned ids are distinct, so this only fires
    on a manual edit. (product_id 0 is "unset" and skipped.)"""
    seen: dict[int, str] = {}
    for name, ov in config.overrides.items():
        bid = int(ov.get("product_id", 0))
        if bid == 0:
            continue
        if bid in seen:
            print("warning: product_id %d is shared by %s and %s; the cross-flash guard "
                  "can't tell them apart - give each board a distinct product_id"
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


def _build_system_info(p, t, app_version, vendor: str, dev: bool = False) -> dict:
    """The derived system/identity info for a target. Written into the ROMFS as
    ``system.json`` (read by the app, OTA or not) and mirrored verbatim into the OTA
    trailer's metadata (so host tools can read it without a ROMFS reader). Composed
    from the lock (provenance) + config (per-board identity); never user-edited.
    ``product_id`` comes from the config when pinned (OTA projects pin it explicitly,
    so it's frozen for the cross-flash guard) and is otherwise auto-derived so even
    a non-OTA image carries a stable product id. ``board_name`` defaults to the
    product name."""
    override = p.config.overrides.get(t.name, {})
    product = p.config.name
    bid = override.get("product_id")
    product_id = int(bid) if bid is not None else derive_product_id(product, t.name)
    return {
        "product": product,
        "board": t.name,
        "product_id": product_id,
        "account_id": p.config.account_id,
        "dev": dev,     # signed with a throwaway --dev key -> visibility flag, never a gate
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

    trailer = Trailer(
        body_size=len(body),
        pad_size=pad_size,
        meta=system_info,
        product_id=int(system_info["product_id"]),
        min_platform_version=int(p.lock.firmware.get("version_code", 0)),
        payload_version=signer.payload_version,
        payload_version_floor=signer.payload_version_floor,
        key_id=signer.key_id,
        sig_alg=signer.sig_alg,
        body_sha256=hashlib.sha256(body).digest(),
    )
    trailer.signature = signer.backend.sign(signed_region(trailer))
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
    key_passphrase_file: str | Path | None = None,
    allow_dev_key: bool = False,
) -> list[BuildResult]:
    project = Path(project)
    try:
        p = load_project(project, firmware=firmware)  # verify=True: refuses on drift
    except ProjectError as e:
        raise BuildError(str(e), exit_code=e.exit_code) from None

    app_dir = Path(app) if app else project / "app"
    copro_dir = project / COPROCESSOR_APP
    out_dir = Path(output) if output else project / "build"
    _warn_product_id_collisions(p.config)

    targets = _select_targets(p.targets, boards)
    if not targets:
        raise BuildError("no matching targets in this project")

    mpy_cmd = mpy.resolve_mpy_cross(p) if compile_py else None
    ota_signer = (_load_signer(p, app_dir, p.config.signing_key_id, require_role="ota",
                               key_passphrase_file=key_passphrase_file, allow_dev_key=allow_dev_key)
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
    mains = [t for t in targets if t.role == "main"]
    coprocs = [t for t in targets if t.role != "main"]
    results = []
    # Coprocessors first: their plain romfs (built from app-coprocessor/) is what the
    # following main build nests into the runtime lib for sync() to write at runtime.
    for t in coprocs:
        results.append(_coprocessor_one(
            p, t, copro_dir, out_dir, ctx, mpy_cmd,
            convert_models=convert_models, mpy_extra=list(mpy_extra or []),
            allow_oversize=allow_oversize, keep_build_dir=keep_build_dir))
    for t in mains:
        inject = _runtime_inject(out_dir, t.name, [c for c in coprocs if c.name == t.name])
        results.append(_build_one(
            p, t, app_dir, out_dir, ctx, mpy_cmd, ota_signer, app_version, vendor,
            convert_models=convert_models, mpy_extra=list(mpy_extra or []),
            allow_oversize=allow_oversize, keep_build_dir=keep_build_dir, inject=inject))
    return results


def _select_targets(targets, boards):
    sel = list(targets)
    if boards:
        sel = [t for t in sel if t.name in boards]
    for t in sel:
        _reject_unsupported(t.name)
    return sel


def _reject_unsupported(name: str) -> None:
    """Refuse a retired board with its graceful message (the firmware crashes at boot)."""
    reason = boards_mod.unsupported_reason(name)
    if reason:
        raise BuildError("board %r is no longer supported: %s" % (name, reason), exit_code=1)


def _target_name(t) -> str:
    """Output basename. The main partition keeps the bare board name; a coprocessor
    partition is suffixed with its role (e.g. ``OPENMV_AE3-coprocessor``)."""
    return t.name if t.role == "main" else "%s-%s" % (t.name, t.role)


def _warn_unset_product_id(t, system_info: dict) -> None:
    if system_info["product_id"] == 0:
        print("warning: %s has product_id 0 (unset); the cross-flash guard is off - set "
              "product_id under [targets.%s] in openmv-ota.toml" % (t.name, t.name),
              file=sys.stderr)


def _build_body(p, t, app_dir, ctx, mpy_cmd, app_version, vendor, *, convert_models, mpy_extra,
                inject=None, dev=False):
    """Stage + compile + convert + system.json + pack. Returns ``(body, system_info,
    tmp_dir)``; the caller is responsible for removing ``tmp_dir``. ``inject(stage)``,
    if given, runs right after staging (before compile) -- used by a main build to nest
    the coprocessor romfs into the staged runtime lib."""
    tmp = Path(tempfile.mkdtemp(prefix="openmv-ota-build-"))
    stage = stage_app(app_dir, tmp / "app")
    if inject is not None:
        inject(stage)

    if mpy_cmd is not None:
        for src in iter_files(stage, (".py",)):
            # The OTA installer ships in the runtime lib's data/ as *source*: install()
            # reads + exec()s it into RAM (it can't run from the slot it's erasing), so
            # it must survive as .py, not be compiled to .mpy.
            if src.parent.name == "data" and src.parent.parent.name == "openmv_ota":
                continue
            mpy.compile_py(mpy_cmd, t.mpy_args + mpy_extra, src, src.with_suffix(".mpy"))
            src.unlink()

    if convert_models and t.npu:
        for model in iter_files(stage, MODEL_SUFFIXES):
            data = convert_model(t, ctx, model)
            if data is not None:
                model.write_bytes(data)

    # The read-only system.json (board identity + provenance) goes into the staged
    # app, so every image - OTA or not - carries it at /rom/system.json.
    system_info = _build_system_info(p, t, app_version, vendor, dev=dev)
    (stage / "system.json").write_text(
        json.dumps(system_info, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    body = build_image(str(stage), t.alignment_rules)
    return body, system_info, tmp


def _partition_name(t) -> str:
    """Human name of a target's partition (e.g. 'High Efficiency Core')."""
    return boards_mod.get_board(t.name).partition(t.partition_index).name


def _runtime_inject(out_dir, board, copro_targets):
    """An ``inject(stage)`` for a *main* build: nest the board's coprocessor romfs into
    the staged runtime lib (``lib/openmv_ota/data/``) and write its sync() manifest, or
    strip that ``data/`` when the board has no coprocessor (so the on-device sync()
    finds nothing -- important when one app/ serves both a coprocessor and a plain
    board). A no-op for a non-OTA project (no runtime lib staged)."""
    def inject(stage):
        lib = stage / "lib" / "openmv_ota"
        if not lib.is_dir():
            return                                   # not an OTA project
        data = lib / "data"
        if not copro_targets:
            # Plain board: drop only the coprocessor resource (nothing to sync) -- keep
            # installer.py + ca.pem, which every OTA image needs.
            (data / "coprocessor.romfs").unlink(missing_ok=True)
            (data / "resources.json").unlink(missing_ok=True)
            return
        data.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_dir / ("%s-coprocessor-romfs.img" % board), data / "coprocessor.romfs")
        entries = [{"file": "coprocessor.romfs", "handler": "partition",
                    "partition": c.partition_index, "name": _partition_name(c)}
                   for c in copro_targets]
        (data / "resources.json").write_text(json.dumps(entries, indent=2) + "\n",
                                             encoding="utf-8")
    return inject


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
               convert_models, mpy_extra, allow_oversize, keep_build_dir, inject=None) -> BuildResult:
    body, system_info, tmp = _build_body(p, t, app_dir, ctx, mpy_cmd, app_version, vendor,
                                         convert_models=convert_models, mpy_extra=mpy_extra,
                                         inject=inject,
                                         dev=bool(ota_signer and ota_signer.backend.is_dev_key))
    try:
        capacity, bound = _capacity(p, t)
        if len(body) > capacity and not allow_oversize:
            raise BuildError(
                "%s image is %d bytes but the %s holds %d (%d over); pass --allow-oversize"
                % (t.name, len(body), bound, capacity, len(body) - capacity), exit_code=1)

        name = _target_name(t)
        if ota_signer is not None:
            from openmv_ota.ota import bundle
            _warn_unset_product_id(t, system_info)
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
    no_account: bool = False,
    key_passphrase_file: str | Path | None = None,
    allow_dev_key: bool = False,
) -> list[BuildResult]:
    """Compose the factory ROMFS partition image per target: golden BACK + initial
    FRONT, both factory-signed, with status sectors and padding. One
    ``<board>-factory-romfs.img`` per main partition. Coprocessor partitions have no
    golden/trial concept, so they get the same plain ``<board>-coprocessor-romfs.img``
    as a regular build -- the image the main core writes into the helper slot.

    The golden BACK slot is **permanent** (never OTA-updatable), so its ``account_id``
    is baked for the life of the device. This refuses to burn an accountless golden
    unless ``no_account`` is set -- so a maker who will register with a shared/hosted
    server can't silently ship ``""`` goldens that later strand on fallback, while a
    self-hoster opts out explicitly."""
    from openmv_ota.ota.keys import FACTORY_KEY_ID_BASE

    project = Path(project)
    try:
        p = load_project(project, firmware=firmware)
    except ProjectError as e:
        raise BuildError(str(e), exit_code=e.exit_code) from None
    if not p.config.ota:
        raise BuildError("factory-romfs needs an OTA project (create with "
                         "`openmv-ota project new --ota`)", exit_code=1)
    if not p.config.account_id and not no_account:
        raise BuildError(
            "no account_id set, and the golden image is permanent -- a device shipped "
            "without an account can't be cleanly moved to a hosted/shared server later "
            "(a golden fallback would strand it). Set [product].account_id in "
            "openmv-ota.toml, or pass --no-account to burn an accountless self-host "
            "golden on purpose.", exit_code=1)

    app_dir = Path(app) if app else project / "app"
    copro_dir = project / COPROCESSOR_APP
    out_dir = Path(output) if output else project / "build"
    _warn_product_id_collisions(p.config)

    targets = _select_targets(p.targets, boards)
    if not targets:
        raise BuildError("no matching targets in this project")

    mpy_cmd = mpy.resolve_mpy_cross(p) if compile_py else None
    key_id = factory_key if factory_key is not None else FACTORY_KEY_ID_BASE
    signer = _load_signer(p, app_dir, key_id, require_role="factory",
                          key_passphrase_file=key_passphrase_file, allow_dev_key=allow_dev_key)
    ctx = ModelContext(
        sdk_home=p.sdk_home, vela_path=p.vela_path, stedgeai_path=p.stedgeai_path,
        vela_optimise=vela_optimise, stedgeai_optimization=stedgeai_optimization,
        vela_extra=list(vela_extra or []), stedgeai_extra=list(stedgeai_extra or []),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    mains = [t for t in targets if t.role == "main"]
    coprocs = [t for t in targets if t.role != "main"]
    results = []
    for t in coprocs:  # plain romfs first; the main factory image nests it (same bytes)
        results.append(_coprocessor_one(
            p, t, copro_dir, out_dir, ctx, mpy_cmd,
            convert_models=convert_models, mpy_extra=list(mpy_extra or []),
            allow_oversize=False, keep_build_dir=keep_build_dir))
    main_names = {t.name for t in mains}
    for t in mains:
        inject = _runtime_inject(out_dir, t.name, [c for c in coprocs if c.name == t.name])
        results.append(_factory_one(
            p, t, app_dir, out_dir, ctx, mpy_cmd, signer,
            signer.app_version, signer.vendor, convert_models=convert_models,
            mpy_extra=list(mpy_extra or []), keep_build_dir=keep_build_dir, inject=inject))
    _record_goldens(p.root, [r for r in results if r.target in main_names], signer)
    return results


def _record_goldens(root: Path, results, signer) -> None:
    """Record each main board's factory image in the ledger so ``build ota-romfs`` can
    resolve the delta base automatically (the golden every device of that board keeps)."""
    from openmv_ota.project import ledger

    for r in results:
        rel = r.output.relative_to(root) if _under(r.output, root) else r.output
        ledger.record_golden(
            root, r.target, version=signer.app_version,
            payload_version=signer.payload_version,
            sha256=hashlib.sha256(r.output.read_bytes()).hexdigest(), path=str(rel))


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _compose_slot(body: bytes, pad: int, rollback_sector: bytes, status_sector: bytes,
                  trailer_bytes: bytes, block: int, slot_size: int) -> bytes:
    """One slot: ``body || 0xFF pad || spare || rollback || status || trailer`` == slot_size
    (the control sectors are the last four blocks; ``spare`` is reserved, all 0xFF)."""
    rollback_block = rollback_sector + b"\xff" * (block - len(rollback_sector))
    spare_block = b"\xff" * block
    trailer_block = trailer_bytes + b"\xff" * (block - len(trailer_bytes))
    slot = body + b"\xff" * pad + spare_block + rollback_block + status_sector + trailer_block
    assert len(slot) == slot_size, (len(slot), slot_size)
    return slot


def _factory_one(p, t, app_dir, out_dir, ctx, mpy_cmd, signer, app_version, vendor, *,
                 convert_models, mpy_extra, keep_build_dir, inject=None) -> BuildResult:
    from openmv_ota.ota import rollback, status

    body, system_info, tmp = _build_body(p, t, app_dir, ctx, mpy_cmd, app_version, vendor,
                                         convert_models=convert_models, mpy_extra=mpy_extra,
                                         inject=inject, dev=signer.backend.is_dev_key)
    try:
        block = geometry.ota_block(t.erase_size)
        front_size = t.front_size
        back_size = t.partition_size - front_size
        overhead = geometry.slot_overhead(t.erase_size)
        front_cap = front_size - overhead
        back_cap = back_size - overhead
        if len(body) > front_cap:  # the smaller slot bounds it
            raise BuildError(
                "%s image is %d bytes but a factory slot holds %d (%d over)"
                % (t.name, len(body), front_cap, len(body) - front_cap), exit_code=1)
        _warn_unset_product_id(t, system_info)
        # seed the anti-rollback floor at the factory version (the device can never be
        # downgraded below it; confirm() advances it as updates are kept).
        floor = rollback.encode_entry(signer.payload_version)

        # FRONT: mountable at first boot (post-OTA-confirmed shape).
        front_pad = front_cap - len(body)
        front = _compose_slot(
            body, front_pad, floor,
            status.build_status_sector(block, pending=True, tried=True, confirmed=True),
            _build_trailer(signer, p, body, system_info, front_pad), block, front_size)
        # BACK: golden / factory state (confirmed only), never trialed.
        back_pad = back_cap - len(body)
        back = _compose_slot(
            body, back_pad, floor,
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


# --- OTA download image (the gzipped FRONT-slot image a server hosts) --------

@dataclass
class OtaImageResult:
    target: str
    partition_index: int
    output: Path        # <board>-ota.img.gz
    image_size: int     # the full FRONT-slot image (uncompressed)
    gz_size: int        # the gzipped artifact actually written


def build_ota_image(
    project: str | Path,
    *,
    output: str | Path | None = None,
    boards: list[str] | None = None,
    firmware: str | Path | None = None,
) -> list[OtaImageResult]:
    """Assemble the gzipped FRONT-slot OTA *download* image(s) from already-built
    bundles -- the artifact a server (e.g. Cloudflare R2) hosts for the device
    ``install()`` to stream in. For each OTA main target it reads the
    ``<board>-romfs.zip`` bundle, lays the body + signed trailer into a full FRONT-slot
    image (blank status sector -- the installer arms PENDING last, after verifying the
    write), gzips it, and writes ``<board>-ota.img.gz``. The gzip collapses the slot's
    0xFF gap to almost nothing, so the artifact is ~body-sized regardless of slot size.

    The signed body+trailer (the bundle) stay the source of truth; this image is a pure,
    regenerable rendering of them for one slot geometry. Run ``build romfs`` first."""
    import gzip

    from openmv_ota.ota import bundle
    from openmv_ota.ota.errors import OtaError

    project = Path(project)
    try:
        p = load_project(project, firmware=firmware)
    except ProjectError as e:
        raise BuildError(str(e), exit_code=e.exit_code) from None
    if not p.config.ota:
        raise BuildError("ota-image needs an OTA project (create with "
                         "`openmv-ota project new --ota`)", exit_code=1)

    out_dir = Path(output) if output else project / "build"
    targets = [t for t in _select_targets(p.targets, boards) if t.role == "main"]
    if not targets:
        raise BuildError("no matching main targets in this project")

    results = []
    for t in targets:
        bundle_path = out_dir / (_target_name(t) + "-romfs.zip")
        if not bundle_path.exists():
            raise BuildError("%s not found - run `openmv-ota build romfs` first"
                             % bundle_path, exit_code=1)
        try:
            body, trailer_bytes = bundle.read_bundle(bundle_path)
        except OtaError as e:
            raise BuildError(str(e), exit_code=1) from None

        block = geometry.ota_block(t.erase_size)
        front_size = t.front_size
        front_cap = front_size - geometry.slot_overhead(t.erase_size)
        if len(body) > front_cap:
            raise BuildError(
                "%s body is %d bytes but a FRONT slot holds %d (rebuild within capacity)"
                % (t.name, len(body), front_cap), exit_code=1)

        # Full FRONT slot, all control sectors blank (0xFF): the installer writes this 1:1,
        # then arms PENDING last. The rollback floor lives in BACK, so FRONT's stays blank.
        image = _compose_slot(body, front_cap - len(body), b"", b"\xff" * block,
                              trailer_bytes, block, front_size)
        gz = gzip.compress(image, mtime=0)            # mtime=0: reproducible artifact
        out_path = out_dir / (_target_name(t) + "-ota.img.gz")
        out_path.write_bytes(gz)
        results.append(OtaImageResult(t.name, t.partition_index, out_path,
                                      len(image), len(gz)))
    return results


# --- signed update manifest (the descriptor the device fetches first) --------

@dataclass
class OtaManifestResult:
    target: str
    partition_index: int
    output: Path        # <board>-manifest.bin
    manifest_size: int
    key_id: int


@dataclass
class OtaDeltaResult:
    output: Path        # <name>.delta.gz
    patch_size: int     # the raw OCDL patch
    gz_size: int        # the gzipped artifact written
    target_size: int    # the reconstructed image size


def _read_maybe_gz(path: Path) -> bytes:
    """Read an image file, transparently gunzipping a ``.gz`` (or gzip-magic) file."""
    data = path.read_bytes()
    if path.suffix == ".gz" or data[:2] == b"\x1f\x8b":
        import gzip
        return gzip.decompress(data)
    return data


def _delta_bytes(base_bytes: bytes, target_bytes: bytes) -> bytes:
    """make_delta + self-check (the patch must reconstruct ``target`` exactly), raising
    BuildError on failure. Shared by ``build_delta`` and ``build_ota_romfs``."""
    from openmv_ota.ota.delta import apply_delta, make_delta
    from openmv_ota.ota.errors import OtaError

    patch = make_delta(base_bytes, target_bytes)
    try:
        if apply_delta(base_bytes, patch) != target_bytes:
            raise BuildError("delta self-check failed: patch does not reconstruct target",
                             exit_code=1)
    except OtaError as e:
        raise BuildError("delta self-check failed: %s" % e, exit_code=1) from None
    return patch


def build_delta(base: str | Path, target: str | Path,
                output: str | Path) -> OtaDeltaResult:
    """Build a gzipped OCDL delta that reconstructs ``target`` from ``base`` (each a raw or
    ``.gz`` image). ``base`` is the device's golden -- the BACK-slot bytes (the back half of
    the golden ``factory-romfs.img``); ``target`` is the new FRONT image (the new
    ``ota.img.gz``). Self-checked: the patch is applied back and must reproduce ``target``
    exactly before it's written, so a published delta always reconstructs its image."""
    import gzip

    target_bytes = _read_maybe_gz(Path(target))
    patch = _delta_bytes(_read_maybe_gz(Path(base)), target_bytes)
    gz = gzip.compress(patch, mtime=0)
    out = Path(output)
    out.write_bytes(gz)
    return OtaDeltaResult(out, len(patch), len(gz), len(target_bytes))


def build_manifest(
    project: str | Path,
    *,
    url_base: str | None = None,
    output: str | Path | None = None,
    app: str | Path | None = None,
    boards: list[str] | None = None,
    firmware: str | Path | None = None,
    delta: str | Path | None = None,
    delta_base_version: str | None = None,
    key_passphrase_file: str | Path | None = None,
    allow_dev_key: bool = False,
) -> list[OtaManifestResult]:
    """Build + sign an update manifest per OTA main target from the already-built
    ``<board>-ota.img.gz`` artifacts -- the descriptor a device's ``install()`` fetches
    *before* it downloads/erases. Each manifest names the reconstructed image's size +
    sha256 and the representations that produce it (the full image; an ``ocdl`` delta when
    ``delta`` is given), and binds product_id / payload_version / min_platform from the image's
    own signed trailer. Signed with the project's OTA key, exactly like the image.
    Representation URLs are **relative filenames by default** (resolved on-device against the
    manifest's own URL, so the signed manifest is host-portable); pass ``url_base`` (an
    absolute ``https://`` dir) to pin absolute URLs instead. The image must be rendered
    first (``build ota-romfs`` does that, then calls here). Pass ``delta`` +
    ``delta_base_version`` (the golden's version) to add the delta rep."""
    import gzip

    from openmv_ota.ota import bundle
    from openmv_ota.ota.delta import target_size as delta_target_size
    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.manifest import DELTA_FORMAT, SCHEMA, Manifest, pack_manifest, signed_region
    from openmv_ota.ota.trailer import parse_trailer
    from openmv_ota.ota.version import decode_app_version, encode_app_version

    if url_base and not url_base.startswith("https://"):
        raise BuildError("manifest --url-base must be an absolute https:// URL", exit_code=1)
    if delta and not delta_base_version:
        raise BuildError("manifest --delta also needs --delta-base-version", exit_code=1)
    _base = url_base.rstrip("/") if url_base else None

    def _rep_url(name):
        return ("%s/%s" % (_base, name)) if _base else name

    project = Path(project)
    try:
        p = load_project(project, firmware=firmware)
    except ProjectError as e:
        raise BuildError(str(e), exit_code=e.exit_code) from None
    if not p.config.ota:
        raise BuildError("manifest needs an OTA project (create with "
                         "`openmv-ota project new --ota`)", exit_code=1)

    app_dir = Path(app) if app else project / "app"
    signer = _load_signer(p, app_dir, p.config.signing_key_id, require_role="ota",
                          key_passphrase_file=key_passphrase_file, allow_dev_key=allow_dev_key)

    out_dir = Path(output) if output else project / "build"
    targets = [t for t in _select_targets(p.targets, boards) if t.role == "main"]
    if not targets:
        raise BuildError("no matching main targets in this project")
    if delta and len(targets) > 1:
        raise BuildError("manifest --delta applies to one board - select it with --board",
                         exit_code=1)

    results = []
    for t in targets:
        name = _target_name(t)
        img_path = out_dir / (name + "-ota.img.gz")
        if not img_path.exists():
            raise BuildError("%s not found - run `openmv-ota build ota-romfs` first"
                             % img_path, exit_code=1)
        image = gzip.decompress(img_path.read_bytes())

        try:
            _body, trailer_bytes = bundle.read_bundle(out_dir / (name + "-romfs.zip"))
            tr = parse_trailer(trailer_bytes)
        except OtaError as e:
            raise BuildError(str(e), exit_code=1) from None

        reps = [{"format": "full", "url": _rep_url(img_path.name),
                 "size": img_path.stat().st_size}]
        if delta:
            delta_path = Path(delta)
            patch = _read_maybe_gz(delta_path)
            try:
                if delta_target_size(patch) != len(image):
                    raise BuildError("delta reconstructs %d bytes but the image is %d "
                                     "(rebuild the delta against this image)"
                                     % (delta_target_size(patch), len(image)), exit_code=1)
            except OtaError as e:
                raise BuildError("bad delta: %s" % e, exit_code=1) from None
            reps.append({"format": DELTA_FORMAT,
                         "url": _rep_url(delta_path.name),
                         "size": delta_path.stat().st_size,
                         "base_payload_version": encode_app_version(delta_base_version)})

        body = {
            "schema": SCHEMA,
            "product_id": tr.product_id,
            "account_id": tr.meta.get("account_id", ""),   # rides in the trailer's JSON meta, not the binary header
            "dev": tr.meta.get("dev", False),              # dev-signed provenance (visibility only)
            "product": tr.meta.get("product", p.config.name),
            "version": decode_app_version(tr.payload_version),
            "payload_version": tr.payload_version,
            "min_platform_version": tr.min_platform_version,
            "size": len(image),
            "sha256": hashlib.sha256(image).hexdigest(),
            "representations": reps,
        }
        m = Manifest(body=body, key_id=signer.key_id, sig_alg=signer.sig_alg)
        m.signature = signer.backend.sign(signed_region(m))
        raw = pack_manifest(m)
        out_path = out_dir / (name + "-manifest.bin")
        out_path.write_bytes(raw)
        results.append(OtaManifestResult(t.name, t.partition_index, out_path,
                                         len(raw), signer.key_id))
    return results


# --- the one cloud-publish verb: image + (optional delta) + signed manifest ---

@dataclass
class OtaRomfsResult:
    target: str
    partition_index: int
    image: Path             # <board>-ota.img.gz
    delta: Path | None      # <board>-ota.delta.gz (when --delta-from supplied a golden)
    manifest: Path          # <board>-manifest.bin
    key_id: int


def build_ota_romfs(
    project: str | Path,
    *,
    delta_from: str | Path | None = None,
    output: str | Path | None = None,
    app: str | Path | None = None,
    boards: list[str] | None = None,
    compile_py: bool = True,
    convert_models: bool = True,
    mpy_extra: list[str] | None = None,
    vela_extra: list[str] | None = None,
    stedgeai_extra: list[str] | None = None,
    vela_optimise: str = "Performance",
    stedgeai_optimization: int = 3,
    firmware: str | Path | None = None,
    allow_republish: bool = False,
    key_passphrase_file: str | Path | None = None,
    allow_dev_key: bool = False,
) -> list[OtaRomfsResult]:
    """Produce the complete **cloud-published** OTA set per main board, from app source in
    one shot (like ``build factory-romfs``): compile + sign the romfs bundle, render the
    gzipped full FRONT-slot image, sign a manifest, and add a delta against the factory
    golden + an ``ocdl`` representation. The golden is resolved automatically from the
    ledger (recorded by ``build factory-romfs``), or pass ``delta_from`` explicitly (a
    ``<board>-factory-romfs.img`` or a directory of them); boards with no golden get
    image + manifest only. The golden is validated (board + older version) and the release
    is recorded -- a non-increasing version is refused unless ``allow_republish``.

    Representation URLs are **relative filenames** -- artifacts are published together and the
    device resolves them against the manifest's own URL, so the signed manifest is
    host-portable (no host baked in). A dynamic update server that serves blobs from a
    different origin sets absolute URLs itself via :func:`build_manifest`'s ``url_base``."""
    import gzip

    from openmv_ota.ota.version import decode_app_version

    project = Path(project)
    try:
        p = load_project(project, firmware=firmware)
    except ProjectError as e:
        raise BuildError(str(e), exit_code=e.exit_code) from None
    if not p.config.ota:
        raise BuildError("ota-romfs needs an OTA project (create with "
                         "`openmv-ota project new --ota`)", exit_code=1)
    out_dir = Path(output) if output else project / "build"
    targets = [t for t in _select_targets(p.targets, boards) if t.role == "main"]
    if not targets:
        raise BuildError("no matching main targets in this project")

    delta_dir = delta_file = None
    if delta_from is not None:
        dp = Path(delta_from)
        if dp.is_dir():
            delta_dir = dp
        elif len(targets) > 1:
            raise BuildError("--delta-from a single file needs one board (--board); pass a "
                             "directory of <board>-factory-romfs.img for several", exit_code=1)
        else:
            delta_file = dp

    from openmv_ota.project import ledger
    app_dir = Path(app) if app else project / "app"
    signer = _load_signer(p, app_dir, p.config.signing_key_id, require_role="ota",
                          key_passphrase_file=key_passphrase_file, allow_dev_key=allow_dev_key)
    new_pv = signer.payload_version

    # 1) compile + sign the romfs bundle, then render the download image(s) from it.
    build_romfs(project, app=app, output=output, boards=boards, compile_py=compile_py,
                convert_models=convert_models, mpy_extra=mpy_extra, vela_extra=vela_extra,
                stedgeai_extra=stedgeai_extra, vela_optimise=vela_optimise,
                stedgeai_optimization=stedgeai_optimization, firmware=firmware,
                key_passphrase_file=key_passphrase_file, allow_dev_key=allow_dev_key)
    build_ota_image(project, output=output, boards=boards, firmware=firmware)

    results = []
    for t in targets:
        name = _target_name(t)
        img_path = out_dir / (name + "-ota.img.gz")
        image = gzip.decompress(img_path.read_bytes())

        last = ledger.last_release(project, name)                   # #6 anti-rollback
        if last and new_pv <= last["payload_version"] and not allow_republish:
            raise BuildError(
                "%s: version %s is not newer than the last published %s -- refusing to "
                "republish/downgrade (pass --allow-republish to override)"
                % (name, signer.app_version, last["version"]), exit_code=1)

        delta_path = base_version = None
        golden = _resolve_golden(project, name, delta_from, delta_file, delta_dir)
        if golden is not None:
            back_tr = _factory_back_trailer(golden)                 # #2 validate the golden
            bid = _product_id_for(p, t)
            if bid and back_tr.product_id and back_tr.product_id != bid:
                raise BuildError("%s: golden %s is for product_id %d, not this board's %d"
                                 % (name, golden, back_tr.product_id, bid), exit_code=1)
            if back_tr.payload_version >= new_pv:
                raise BuildError(
                    "%s: golden version %s is not older than this release %s (deltas go "
                    "golden -> new)" % (name, decode_app_version(back_tr.payload_version),
                                        signer.app_version), exit_code=1)
            base_version = decode_app_version(back_tr.payload_version)
            patch = _delta_bytes(golden.read_bytes()[t.front_size:], image)   # BACK golden bytes
            delta_path = out_dir / (name + "-ota.delta.gz")
            delta_path.write_bytes(gzip.compress(patch, mtime=0))

        [mres] = build_manifest(project, output=output, app=app,
                                boards=[name], firmware=firmware,
                                delta=delta_path, delta_base_version=base_version,
                                key_passphrase_file=key_passphrase_file, allow_dev_key=allow_dev_key)
        ledger.record_release(project, name, version=signer.app_version, payload_version=new_pv,
                              sha256=hashlib.sha256(image).hexdigest(), key_id=mres.key_id)
        results.append(OtaRomfsResult(t.name, t.partition_index, img_path, delta_path,
                                      mres.output, mres.key_id))
    return results


def _resolve_golden(project, name, delta_from, delta_file, delta_dir):
    """The factory image to diff against, or None (-> full image only, with a warning).
    Explicit ``--delta-from`` wins; otherwise the golden recorded in the ledger."""
    if delta_from is not None:
        fac = delta_file or (delta_dir / (name + "-factory-romfs.img"))
        if fac.exists():
            return fac
        print("warning: no factory golden for %s at %s - full image only"
              % (name, fac), file=sys.stderr)
        return None
    from openmv_ota.project import ledger
    g = ledger.golden_for(project, name)
    if g is None:
        return None
    path = Path(project) / g["path"]
    if not path.exists():
        print("warning: %s's recorded golden is missing at %s - full image only (keep your "
              "factory images)" % (name, path), file=sys.stderr)
        return None
    return path


def _factory_back_trailer(golden: Path):
    from openmv_ota.ota import partition
    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.trailer import parse_trailer
    try:
        back = next((tr for lbl, _b, tr in partition.slots(golden.read_bytes())
                     if lbl == "BACK"), None)
        if back is None:
            raise OtaError("no BACK slot")
        return parse_trailer(back)
    except OtaError as e:
        raise BuildError("%s is not a usable factory image: %s" % (golden, e),
                         exit_code=1) from None


def _product_id_for(p, t) -> int:
    ov = p.config.overrides.get(t.name, {})
    bid = ov.get("product_id")
    return int(bid) if bid is not None else derive_product_id(p.config.name, t.name)
