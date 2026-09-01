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
    from openmv_ota.project.backends import read_backends
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
                               backend=read_backends(p.root).get(key_id), passphrase_provider=provider)
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
    OTA partition gets a slot less its four control sectors -- half the partition under
    A/B, all of it under SINGLE; a coprocessor partition is always a plain romfs that fills
    the whole partition.

    MODE-AWARE, because ``front_size`` is an A/B concept. In SINGLE mode there is no
    front/back split and ``front_size`` is 0, so the A/B arithmetic returned a NEGATIVE
    capacity -- OPENMV2 reported "the OTA slot holds -16384" and every image was over budget
    by construction, which is to say `build romfs` could not produce an image for any
    one-erase-sector board. Those are exactly the boards SINGLE exists for."""
    if project.config.ota and target.role == "main":
        if geometry.derive_mode(target.partition_size, target.erase_size) == geometry.SINGLE:
            return (geometry.single_body_capacity(target.partition_size, target.erase_size),
                    "OTA slot (single image)")
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
    if p.config.ota:
        _warn_stale_device_lib(p)
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


_DEVICE_LIB = Path(__file__).parent / "device" / "openmv_ota"


def _warn_stale_device_lib(p) -> None:
    """Warn when the project's scaffolded ``app/lib/openmv_ota`` differs from this
    tool's bundled device lib. The lib is the user's to edit -- but when the
    difference is a STALE SCAFFOLD (the tool moved; the copy did not), the device
    runs old code while everything else is new. On the bench that mismatch cost
    three debugging cycles before anyone thought to compare the copies."""
    lib = Path(p.root) / "app" / "lib" / "openmv_ota"
    if not lib.is_dir():
        return
    stale = [str(src.relative_to(_DEVICE_LIB))
             for src in sorted(_DEVICE_LIB.rglob("*.py"))
             if (lib / src.relative_to(_DEVICE_LIB)).exists()
             and (lib / src.relative_to(_DEVICE_LIB)).read_bytes() != src.read_bytes()]
    if stale:
        print("warning: app/lib/openmv_ota differs from this tool's bundled device lib "
              "(%s). Yours to edit on purpose -- but if this is a stale scaffold, copy "
              "the bundled files over from the installed package "
              "(openmv_ota/build/device/openmv_ota/)." % ", ".join(stale),
              file=sys.stderr)


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
            # The app entry point must survive as main.py: the firmware auto-runs it with
            # pyexec_file_if_exists("main.py") from the mounted slot (cwd = the romfs), and
            # that looks for main.py by name -- a compiled main.mpy would never launch, so
            # the app (and its OTA confirm) would never run on a real boot.
            if src.parent == stage and src.name == "main.py":
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
        installer = data / "installer.py"
        if installer.exists():
            # The installer ships as SOURCE (exec'd into RAM on-device), so its repo
            # comments would otherwise ship too -- ~70% of the file. Strip at pack
            # time: same code, a third of the flash and install-time RAM.
            from .pystrip import strip_python_source
            installer.write_text(strip_python_source(installer.read_text(encoding="utf-8")),
                                 encoding="utf-8")
        if not copro_targets:
            # Plain board: drop only the coprocessor resource (nothing to sync) -- keep
            # installer.py, which every OTA image needs (the trust store rides in the
            # firmware; a data/ca.pem here is a project's deliberate override).
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


# --- provisioning image (both slots, factory-signed) ------------------------

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
    """Compose the PROVISIONING ROMFS partition image per target -- the image flashed at
    manufacture. Under A/B that is the SAME factory-signed image written into both slots with
    different install counters, so the device is fallback-capable from its first boot; under
    single-image it is one slot spanning the partition. One ``<board>-factory-romfs.img`` per
    main partition. Coprocessor partitions have no slot/trial concept, so they get the same
    plain ``<board>-coprocessor-romfs.img`` as a regular build -- the image the main core
    writes into the helper slot.

    ``no_account`` silences the accountless warning. It used to be a required opt-out, because
    the golden slot was permanent and an accountless golden was baked for the life of the
    device; with both slots updatable an account can be introduced by a later release."""
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
        # v1 made this an ERROR because the golden slot was permanent: an accountless golden
        # was burned for the life of the device and would strand it on fallback. Under A/B no
        # slot is permanent -- both are updatable, and an account can be introduced by a later
        # release -- so the hard gate is no longer honest. It stays a loud warning, because
        # shipping without an account is still a decision worth making deliberately.
        print("warning: no account_id set. Both slots are updatable, so this can be fixed by a "
              "later release, but every device you ship until then is accountless. Set "
              "[product].account_id in openmv-ota.toml, or pass --no-account to silence this.",
              file=sys.stderr)

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


def _compose_slot(body: bytes, pad: int, status_sector: bytes,
                  trailer_bytes: bytes, block: int, slot_size: int) -> bytes:
    """One slot: ``body || 0xFF pad || status || trailer`` == slot_size
    (the two control sectors are the last two blocks)."""
    trailer_block = trailer_bytes + b"\xff" * (block - len(trailer_bytes))
    slot = body + b"\xff" * pad + status_sector + trailer_block
    assert len(slot) == slot_size, (len(slot), slot_size)
    return slot


def provision_slots(partition_size: int, erase_size: int, single_image: bool = False):
    """The slots a provisioning image lays down, in image order: ``[(size, counter), ...]``.

    A/B writes the SAME image into both slots with different install counters, so a device is
    fallback-capable from its very first boot rather than after its first successful update.
    Slot A gets the higher counter, which makes it the one that boots and makes B the target of
    the first update -- decided by exactly the rule that decides every later update, not by a
    factory-only special case. SINGLE lays down one slot spanning the partition."""
    mode = geometry.resolve_mode(partition_size, erase_size, single_image)
    if mode == geometry.SINGLE:
        return [(partition_size, 1)]
    slot = geometry.front_size(partition_size, erase_size)
    return [(slot, 2), (slot, 1)]        # EQUAL sizes: one OTA image must fit either slot


def _factory_one(p, t, app_dir, out_dir, ctx, mpy_cmd, signer, app_version, vendor, *,
                 convert_models, mpy_extra, keep_build_dir, inject=None) -> BuildResult:
    from openmv_ota.ota import status

    body, system_info, tmp = _build_body(p, t, app_dir, ctx, mpy_cmd, app_version, vendor,
                                         convert_models=convert_models, mpy_extra=mpy_extra,
                                         inject=inject, dev=signer.backend.is_dev_key)
    try:
        # CONTROL sectors are always control_block()-sized (4 KiB), never the erase
        # block: on a one-big-sector board (an F427's 128 KiB internal sectors) sizing
        # them to the erase block composed 4x128 KiB of "control" into a 128 KiB slot.
        # Invisible on external-NOR boards, whose erase block IS 4 KiB.
        block = geometry.control_block()
        overhead = geometry.slot_overhead(t.erase_size)
        slots = provision_slots(t.partition_size, t.erase_size, p.config.single_image)
        smallest = min(size for size, _counter in slots) - overhead
        if len(body) > smallest:   # the smaller slot bounds it
            raise BuildError(
                "%s image is %d bytes but a slot holds %d (%d over)"
                % (t.name, len(body), smallest, len(body) - smallest), exit_code=1)
        _warn_unset_product_id(t, system_info)
        # Both slots ship CONFIRMED: they have nothing to prove, having never been trialed.
        # There is no golden shape and no golden slot -- the difference between the two is one
        # number, the install counter, and after the first update they are just two images with
        # two counters. That is the whole point of dropping the factory special case: the device
        # runs one code path from provisioning onward.
        image = b""
        for size, counter in slots:
            pad = size - overhead - len(body)
            # the status sector ships CONFIRMED + the counter + the anti-rollback floor
            # seeded at the factory version (the device can never be downgraded below it;
            # confirm() raises it as updates are kept)
            image += _compose_slot(
                body, pad,
                status.build_status_sector(block, pending=False, tried=False, confirmed=True,
                                           counter=counter, stride=t.control_stride,
                                           floor_version=signer.payload_version),
                _build_trailer(signer, p, body, system_info, pad), block, size)

        name = _target_name(t)
        out_path = out_dir / (name + "-factory-romfs.img")
        out_path.write_bytes(image)

        # THE PUBLISHABLE FORM OF THE FACTORY BYTES -- build/factory/<board>-ota.img.gz +
        # a manifest signed with the same factory key. A factory-fresh fleet's first delta
        # base is exactly this body, and the server-driven base machinery (--delta-fleet /
        # `client release bases`) only knows STORED RELEASES -- so publish this pair right after
        # manufacture (`client release publish . -b <board> -o build/factory`) and the fleet's
        # first OTA can be a delta. Rendered here from the very bytes just composed, so
        # the published base can never drift from the flashed one.
        import gzip as _gzip

        from openmv_ota.ota.manifest import Manifest, pack_manifest, signed_region
        from openmv_ota.ota.trailer import parse_trailer

        pub_dir = out_dir / "factory"
        pub_dir.mkdir(parents=True, exist_ok=True)
        size0, _counter0 = slots[0]
        pad0 = size0 - overhead - len(body)
        trailer0 = _build_trailer(signer, p, body, system_info, pad0)
        dl = _compose_slot(body, pad0, b"\xff" * block, trailer0, block, size0)
        single = size0 == t.partition_size
        gz = _gzip_small(dl, SINGLE_WBITS) if single else _gzip.compress(dl, mtime=0)
        img_path = pub_dir / (name + "-ota.img.gz")
        img_path.write_bytes(gz)
        reps = [{"format": "full", "url": img_path.name, "size": len(gz)}]
        if single:
            reps[0]["wbits"] = SINGLE_WBITS
        m = Manifest(body=_manifest_body(p, parse_trailer(trailer0), dl, reps),
                     key_id=signer.key_id, sig_alg=signer.sig_alg)
        m.signature = signer.backend.sign(signed_region(m))
        (pub_dir / (name + "-manifest.bin")).write_bytes(pack_manifest(m))

        return BuildResult(t.name, t.partition_index, out_path, len(body), smallest,
                           bound="factory slot", ota=True,
                           build_dir=tmp if keep_build_dir else None)
    finally:
        if not keep_build_dir:
            shutil.rmtree(tmp, ignore_errors=True)


def _gzip_small(data: bytes, wbits: int) -> bytes:
    """A gzip container holding a deflate stream with a SMALL window (2**wbits), for
    small-heap devices: DeflateIO's inflate window is 2**wbits bytes of RAM, and the
    default 32 KiB (wbits 15) does not fit a classic's ~40 KB heap. The gzip header
    does not carry the window size, so the manifest records ``wbits`` and the
    installer passes it through. Any host gzip reads this fine (a smaller window is
    always valid); ``mtime=0`` for reproducibility, like gzip.compress."""
    import struct
    import zlib
    co = zlib.compressobj(9, zlib.DEFLATED, -wbits)
    deflated = co.compress(data) + co.flush()
    return (b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff" + deflated
            + struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF))


# --- OTA download image (the gzipped slot-sized image a server hosts) --------

SINGLE_WBITS = 12     # 4 KiB inflate window: measured on the bench -- a classic's heap after
#                        the app + runtime lib cannot give DeflateIO 8 KiB contiguous


@dataclass
class OtaImageResult:
    target: str
    partition_index: int
    output: Path        # <board>-ota.img.gz
    image_size: int     # the full slot-sized image (uncompressed)
    gz_size: int        # the gzipped artifact actually written
    wbits: int | None = None   # small inflate window used (SINGLE mode); None = default 32 KiB


def build_ota_image(
    project: str | Path,
    *,
    output: str | Path | None = None,
    boards: list[str] | None = None,
    firmware: str | Path | None = None,
) -> list[OtaImageResult]:
    """Assemble the gzipped slot-sized OTA *download* image(s) from already-built
    bundles -- the artifact a server (e.g. Cloudflare R2) hosts for the device
    ``install()`` to stream in. For each OTA main target it reads the
    ``<board>-romfs.zip`` bundle, lays the body + signed trailer into a full slot-sized
    image (blank control sectors -- the installer stamps the counter and arms PENDING last,
    after verifying the write), gzips it, and writes ``<board>-ota.img.gz``. ONE image serves
    either slot, which is why the two A/B slots are built the same size. The gzip collapses the slot's
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

        # MODE-AWARE, like _capacity(): front_size is an A/B concept and is 0 in
        # SINGLE mode, where the one slot spans the partition. The control block is
        # always 4 KiB (see the factory composer).
        block = geometry.control_block()
        if geometry.resolve_mode(t.partition_size, t.erase_size,
                                 p.config.single_image) == geometry.SINGLE:
            slot_size = t.partition_size
        else:
            slot_size = t.front_size
        slot_cap = slot_size - geometry.slot_overhead(t.erase_size)
        if len(body) > slot_cap:
            raise BuildError(
                "%s body is %d bytes but a slot holds %d (rebuild within capacity)"
                % (t.name, len(body), slot_cap), exit_code=1)

        # A full slot, the status sector blank (0xFF): the installer writes this 1:1, then
        # seeds the carried floor, stamps the install counter and arms PENDING. The floor is
        # device state, carried forward from what the device already had -- an image cannot
        # know it.
        image = _compose_slot(body, slot_cap - len(body), b"\xff" * block,
                              trailer_bytes, block, slot_size)
        single = slot_size == t.partition_size
        if single:
            # Small-heap boards: an 8 KiB inflate window (wbits 13) instead of the
            # default 32 KiB, recorded in the manifest so the device allocates the same.
            gz = _gzip_small(image, SINGLE_WBITS)
        else:
            gz = gzip.compress(image, mtime=0)        # mtime=0: reproducible artifact
        out_path = out_dir / (_target_name(t) + "-ota.img.gz")
        out_path.write_bytes(gz)
        results.append(OtaImageResult(t.name, t.partition_index, out_path,
                                      len(image), len(gz),
                                      wbits=SINGLE_WBITS if single else None))
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
    ``.gz`` image). ``base`` is the image the device is running -- slot B's bytes (the second
    half of its ``factory-romfs.img``); ``target`` is the new slot image (the new
    ``ota.img.gz``). Self-checked: the patch is applied back and must reproduce ``target``
    exactly before it's written, so a published delta always reconstructs its image."""
    import gzip

    target_bytes = _read_maybe_gz(Path(target))
    patch = _delta_bytes(_read_maybe_gz(Path(base)), target_bytes)
    gz = gzip.compress(patch, mtime=0)
    out = Path(output)
    out.write_bytes(gz)
    return OtaDeltaResult(out, len(patch), len(gz), len(target_bytes))


def _manifest_body(p, tr, image: bytes, reps: list[dict]) -> dict:
    """The manifest's signed body dict for ``image`` (the decompressed download image),
    identity bound from its trailer ``tr``. Shared by ``build_manifest`` and the factory
    build's publishable pair, so the two can never drift on a field."""
    from openmv_ota.ota.manifest import SCHEMA
    from openmv_ota.ota.version import decode_app_version

    return {
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


def build_manifest(
    project: str | Path,
    *,
    url_base: str | None = None,
    output: str | Path | None = None,
    app: str | Path | None = None,
    boards: list[str] | None = None,
    firmware: str | Path | None = None,
    deltas: list | None = None,
    key_passphrase_file: str | Path | None = None,
    allow_dev_key: bool = False,
) -> list[OtaManifestResult]:
    """Build + sign an update manifest per OTA main target from the already-built
    ``<board>-ota.img.gz`` artifacts -- the descriptor a device's ``install()`` fetches
    *before* it downloads/erases. Each manifest names the reconstructed image's size +
    sha256 and the representations that produce it (the full image; an ``ocdl`` delta when
    ``delta`` is given), and binds product_id / payload_version / min_platform from the image's
    own signed trailer. A manifest may carry SEVERAL deltas, one per base version -- the
    device picks the one whose base matches the release it is running, which under A/B varies
    across a fleet mid-rollout. Signed with the project's OTA key, exactly like the image.
    Representation URLs are **relative filenames by default** (resolved on-device against the
    manifest's own URL, so the signed manifest is host-portable); pass ``url_base`` (an
    absolute ``https://`` dir) to pin absolute URLs instead. The image must be rendered
    first (``build ota-romfs`` does that, then calls here). Pass ``deltas`` (a list of
    ``(path, base_version, base_body_sha256)``) to add delta reps -- the base sha names the
    exact bytes the patch applies against, and the device refuses a delta without it."""
    import gzip

    from openmv_ota.ota import bundle
    from openmv_ota.ota.delta import target_size as delta_target_size
    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.manifest import DELTA_FORMAT, Manifest, pack_manifest, signed_region
    from openmv_ota.ota.trailer import parse_trailer
    from openmv_ota.ota.version import encode_app_version

    if url_base and not url_base.startswith("https://"):
        raise BuildError("manifest --url-base must be an absolute https:// URL", exit_code=1)
    delta_list = list(deltas or [])
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
    if delta_list and len(targets) > 1:
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
        if geometry.resolve_mode(t.partition_size, t.erase_size,
                                 p.config.single_image) == geometry.SINGLE:
            # A single-image device inflates with a SMALL window (the renderer
            # compressed with the same 2**SINGLE_WBITS one -- see _gzip_small); the
            # signed manifest is how the device learns the size, since a gzip header
            # does not carry it.
            reps[0]["wbits"] = SINGLE_WBITS
        for entry in delta_list:
            delta_path, base_version, base_body_sha = Path(entry[0]), entry[1], entry[2]
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
                         "base_payload_version": encode_app_version(base_version),
                         # the base's trailer body_sha256: the device applies this delta
                         # only when its RUNNING slot carries these exact bytes (version
                         # alone stopped being an identity when --allow-republish arrived)
                         "base_body_sha256": base_body_sha})

        body = _manifest_body(p, tr, image, reps)
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
    deltas: list           # <board>-ota.delta-<base>.gz, one per base version (may be empty)
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
    delta_fleet: bool = False,
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

    delta_dirs: list[Path] = []
    delta_files: list[Path] = []
    if delta_from is not None:
        entries = delta_from if isinstance(delta_from, (list, tuple)) else [delta_from]
        for entry in entries:
            dp = Path(entry)
            if dp.is_dir():
                delta_dirs.append(dp)
            elif len(targets) > 1:
                raise BuildError("--delta-from a single file needs one board (--board); pass "
                                 "a directory of <board>-factory-romfs.img for several",
                                 exit_code=1)
            else:
                delta_files.append(dp)

    from openmv_ota.project import ledger
    app_dir = Path(app) if app else project / "app"
    signer = _load_signer(p, app_dir, p.config.signing_key_id, require_role="ota",
                          key_passphrase_file=key_passphrase_file, allow_dev_key=allow_dev_key)
    new_pv = signer.payload_version

    fleet = {}
    if delta_fleet:
        # Ask the server which (version, bytes) bases the fleet is RUNNING and pull the
        # matching stored releases -- the automated spelling of `client release bases` + a curated
        # --delta-from. Composes with any explicit --delta-from entries.
        from openmv_ota.client.errors import ClientError
        try:
            fleet = _fetch_fleet_bases(p, targets, new_pv, out_dir / "bases")
        except ClientError as e:               # missing creds / server error -> a BUILD error,
            raise BuildError("--delta-fleet: %s" % e, exit_code=1) from None  # not a traceback
        delta_dirs.append(out_dir / "bases")

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

        # ONE DELTA PER BASE. The device picks by exact base match, so a release that ships
        # only a delta-from-provisioning reaches nobody who has already updated -- which under
        # A/B is everybody, because the base is whatever release the device is running.
        deltas = []
        seen_bases = set()
        for base_path in _resolve_bases(project, name, delta_from, delta_files, delta_dirs):
            base_body, base_tr = _base_slot(base_path, t.erase_size)
            bid = _product_id_for(p, t)
            if bid and base_tr.product_id and base_tr.product_id != bid:
                raise BuildError("%s: delta base %s is for product_id %d, not this board's %d"
                                 % (name, base_path, base_tr.product_id, bid), exit_code=1)
            if base_tr.payload_version >= new_pv:
                raise BuildError(
                    "%s: delta base %s is version %s, not older than this release %s (deltas "
                    "go old -> new)" % (name, base_path,
                                        decode_app_version(base_tr.payload_version),
                                        signer.app_version), exit_code=1)
            base_version = decode_app_version(base_tr.payload_version)
            if base_version in seen_bases:      # two files for one version -> one rep
                continue
            seen_bases.add(base_version)
            # The base is the BODY REGION only; the target is the full slot image. That
            # asymmetry is the fix for the per-device control sectors -- no delta op can
            # reference them, so the reconstruction is identical on every device.
            patch = _delta_bytes(base_body, image)
            delta_path = out_dir / ("%s-ota.delta-%s.gz" % (name, base_version))
            delta_path.write_bytes(gzip.compress(patch, mtime=0))
            deltas.append((delta_path, base_version, base_tr.body_sha256.hex()))
        if delta_fleet:
            _warn_fleet_coverage(name, _product_id_for(p, t), fleet, new_pv,
                                 {sha for _p, _v, sha in deltas})

        [mres] = build_manifest(project, output=output, app=app,
                                boards=[name], firmware=firmware,
                                deltas=deltas,
                                key_passphrase_file=key_passphrase_file, allow_dev_key=allow_dev_key)
        ledger.record_release(project, name, version=signer.app_version, payload_version=new_pv,
                              sha256=hashlib.sha256(image).hexdigest(), key_id=mres.key_id)
        results.append(OtaRomfsResult(t.name, t.partition_index, img_path,
                                      [d for d, _v, _sha in deltas],
                                      mres.output, mres.key_id))
    return results


def _fetch_fleet_bases(p, targets, new_pv, out_dir):
    """``--delta-fleet``: ask the update server which (version, bytes) bases the fleet is
    RUNNING (``GET /fleet/bases``, fed by the check-in slots' body_sha256) and pull the
    matching stored releases into ``out_dir`` under the ``<board>-base-<version>.img.gz``
    names ``--delta-from <dir>`` reads. Only bases OLDER than this release are pulled (a
    device already at/above it takes nothing). Always re-downloaded, never cached: a cached
    file could silently be a republished version's OLD bytes -- the exact confusion the
    base-identity sha exists to kill. Client credentials resolve exactly as `client` verbs
    do (flag-less here: env or the saved login). Returns ``{product_id: [fleet rows]}`` for
    the post-build coverage warning."""
    from openmv_ota.client import config as client_config
    from openmv_ota.client.api import Api

    cfg = client_config.resolve(None, None)
    api = Api(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    fleet = {}
    for t in targets:
        pid = _product_id_for(p, t)
        if pid in fleet or not pid:
            continue
        rows = api.fleet_bases(pid)["bases"]
        fleet[pid] = rows
        releases = {r["payload_version"]: r
                    for r in api.releases(pid, limit=500)["releases"]}
        for row in rows:
            if not row["body_sha256"] or row["payload_version"] >= new_pv:
                continue                       # sha-less devices take full; newer never patch
            rel = releases.get(row["payload_version"])
            if rel is None:
                print("warning: %d device(s) run %s but no stored release matches (pruned?) "
                      "-- they will take the full image"
                      % (row["devices"], row["version"]), file=sys.stderr)
                continue
            path = out_dir / ("%s-base-%s.img.gz" % (_target_name(t), rel["version"]))
            path.write_bytes(api.release_image(rel["release_id"]))
    return fleet


def _warn_fleet_coverage(name, pid, fleet, new_pv, built_shas):
    """After the deltas are built: name the fleet groups this release CANNOT reach by delta.
    A row whose sha none of the built bases carries is a republish split (the store holds
    different bytes than those devices run) -- safe either way (they take the full image),
    but silence here would read as covered."""
    for row in fleet.get(pid, ()):
        if not row["body_sha256"] or row["payload_version"] >= new_pv:
            continue                          # sha-less -> full by design; newer never patches
        if row["body_sha256"] not in built_shas:
            print("warning: %s: %d device(s) run %s with bytes no delta base matches "
                  "(republished version?) -- they will take the full image"
                  % (name, row["devices"], row["version"]), file=sys.stderr)


def _resolve_bases(project, name, delta_from, delta_files, delta_dirs) -> list[Path]:
    """Every image to build a delta against, or ``[]`` (-> full image only).

    Explicit ``--delta-from`` wins and is REPEATABLE, because under A/B one base is not
    enough: a device's delta base is the release it is *running*, so a fleet mid-rollout is
    spread across several versions and a single base reaches only the devices still on it.
    Each entry is a provisioning image, a previous release's ``-ota.img.gz``, or a directory
    holding the per-board file. With none given, the ledger's recorded provisioning image is
    the one base -- which is right for a fleet that has never updated, and is why publishing
    the previous release as a base is worth doing explicitly. ``--delta-fleet`` contributes
    its downloads as one more directory, so its presence also selects the explicit path."""
    if delta_from is not None or delta_files or delta_dirs:
        out = []
        for f in delta_files:
            out.append(f)
        for d in delta_dirs:
            # A directory contributes the board's provisioning image AND any bases pulled back
            # from the server by `client release bases` (<board>-base-<version>.img.gz). Matching a
            # fixed pattern rather than "every file here" keeps an output directory full of
            # unrelated artifacts from being fed to the delta builder.
            found = sorted(d.glob(name + "-base-*.img.gz"))
            fac = d / (name + "-factory-romfs.img")
            if fac.exists():
                found.append(fac)
            if found:
                out.extend(found)
            else:
                print("warning: no delta base for %s in %s - skipping that directory"
                      % (name, d), file=sys.stderr)
        if not out:
            print("warning: no delta base for %s - full image only" % name, file=sys.stderr)
        return out
    from openmv_ota.project import ledger
    g = ledger.golden_for(project, name)
    if g is None:
        return []
    path = Path(project) / g["path"]
    if not path.exists():
        print("warning: %s's recorded provisioning image is missing at %s - full image only "
              "(keep your factory images)" % (name, path), file=sys.stderr)
        return []
    return [path]


def _base_slot(path: Path, erase_size: int):
    """``(body_region_bytes, trailer)`` for a delta base image.

    Accepts both shapes a base can take: a **provisioning image** (two slots -- take the
    second, they are identical bar the install counter) and a **single slot image**, i.e. a
    previous release's ``-ota.img.gz``. Which it is, is read from the image rather than from
    the filename, so publishing "a delta from last release" needs no new flag.

    Returns the BODY REGION only. The control sectors are excluded deliberately and that is
    the whole point of this function -- see ``geometry.delta_base_len``."""
    from openmv_ota.ota import partition
    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.trailer import parse_trailer

    raw = _read_maybe_gz(path)
    try:
        found = partition.slots(raw)
        if not found:
            raise OtaError("no signed trailer")
        # two slots -> a provisioning image, and its slots are equal-sized
        _label, _body, trailer_bytes = found[-1]
        slot_size = len(raw) // 2 if len(found) == 2 else len(raw)
        start = slot_size if len(found) == 2 else 0
        return (raw[start:start + geometry.delta_base_len(slot_size, erase_size)],
                parse_trailer(trailer_bytes))
    except OtaError as e:
        raise BuildError("%s is not a usable delta base: %s" % (path, e),
                         exit_code=1) from None


def _product_id_for(p, t) -> int:
    ov = p.config.overrides.get(t.name, {})
    bid = ov.get("product_id")
    return int(bid) if bid is not None else derive_product_id(p.config.name, t.name)
