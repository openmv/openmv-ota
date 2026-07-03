"""Project orchestration: create, load, resolve the snapshot, diff, and setup."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from openmv_ota import __version__
from openmv_ota.ota import geometry
from openmv_ota.ota.algorithms import ES256, algorithm_for
from openmv_ota.romfs import boards as boards_mod

from . import cache, config as config_mod, gitrepo, lock as lock_mod, sdk_install
from .config import LOCAL_NAME, OtaConfig
from .errors import ProjectError
from .resolve import firmware as fw_res
from .resolve import micropython as mp_res
from .resolve import sdk as sdk_res
from .resolve.board import ResolvedBoard, resolve_board
from .resolve.submodules import resolve_submodules

GENERATED_BY = "openmv-ota %s" % __version__

# The CA root bundle the device verifies OTA download TLS against, fetched fresh when
# an OTA project is created (the curl/Mozilla bundle). No offline fallback: creating an
# OTA project needs network anyway (the SDK), and a stale vendored snapshot would rot.
CA_BUNDLE_URL = "https://curl.se/ca/cacert.pem"

# The project folder holding a coprocessor core's app (a slaved second partition,
# e.g. AE3's M55_HE). Built as a plain romfs and written by the main core.
COPROCESSOR_APP = "app-coprocessor"

_GITIGNORE = """\
# openmv-ota: machine-local settings (never commit)
openmv-ota.local.toml

# private signing material
keys/*.pem
keys/*.key
keys/private/
keys-backup.enc

# build artefacts
build/
releases/*.bin
"""


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / config_mod.CONFIG_NAME

    @property
    def lock(self) -> Path:
        return self.root / lock_mod.LOCK_NAME

    @property
    def local(self) -> Path:
        return self.root / LOCAL_NAME

    @property
    def gitignore(self) -> Path:
        return self.root / ".gitignore"

    @property
    def readme(self) -> Path:
        return self.root / "README.md"

    @property
    def trusted_keys(self) -> Path:
        return self.root / "keys" / "trusted_keys.json"

    @property
    def private_keys_dir(self) -> Path:
        return self.root / "keys" / "private"

    @property
    def app_dir(self) -> Path:
        return self.root / "app"

    @property
    def app_settings(self) -> Path:
        return self.root / "app" / "settings.json"

    @property
    def coprocessor_app_dir(self) -> Path:
        return self.root / COPROCESSOR_APP


# --- snapshot resolution ----------------------------------------------------

def resolve_snapshot(
    repo: Path,
    config: OtaConfig,
    *,
    sdk_home_override: Path | None,
    config_digest: str,
    now: str,
) -> tuple[lock_mod.Lock, list[str]]:
    """Resolve a full lock from the firmware checkout. Does not enforce SDK
    presence (callers do that for new/sync via :func:`ensure_sdk`)."""
    if not gitrepo.is_git_repo(repo):
        raise ProjectError("not a git repository: %s" % repo)

    ver = fw_res.resolve_firmware_version(repo)
    mp = mp_res.resolve_micropython(repo)
    sdk_info = sdk_res.resolve_sdk(repo, sdk_home_override)
    vela = sdk_res.resolve_vela(sdk_info.home)
    stedgeai = sdk_res.resolve_stedgeai(sdk_info.home)
    mpy_cross = sdk_res.resolve_mpy_cross(repo, mp)

    warnings: list[str] = []
    resolved: list[dict] = []
    for name in config.boards:
        override = config.overrides.get(name, {})
        try:
            bcfg = boards_mod.get_board(name)
        except KeyError as e:
            raise ProjectError(str(e)) from None
        if bcfg.unsupported:                         # a retired board -- can't be a target
            raise ProjectError("board %r is no longer supported: %s"
                               % (name, bcfg.unsupported), exit_code=1)
        # Every partition the board declares is a target -- there is nothing to
        # configure per partition: a coprocessor partition is slaved to the main one
        # (the main core owns it), so the tool always builds them all. A
        # ``partition_size`` override only makes sense for the main OTA partition.
        for part in bcfg.partitions:
            per = ({k: v for k, v in override.items() if k in ("partition_size",)}
                   if part.role == "main" else {})
            rb, w = resolve_board(repo, name, part.index, per)
            warnings.extend(w)
            resolved.append(asdict(rb))
    resolved.sort(key=lambda r: (r["name"], r["partition_index"]))

    lock = lock_mod.Lock(
        generated_by=GENERATED_BY,
        generated_at=now,
        config_digest=config_digest,
        ota=config.ota,
        firmware={
            "version": ver.string,
            "version_parts": {"major": ver.major, "minor": ver.minor, "patch": ver.patch},
            "version_code": ver.code,
            "remote": gitrepo.remote_url(repo),
            "commit": gitrepo.head_commit(repo),
            "branch": gitrepo.current_branch(repo),
            "describe": gitrepo.describe(repo),
            "dirty": gitrepo.is_dirty(repo),
        },
        micropython={
            "commit": gitrepo.head_commit(repo / mp_res.MICROPYTHON_SUBPATH),
            "version": mp.version,
            "prerelease": mp.prerelease,
            "mpy_abi_version": mp.mpy_abi_version,
            "mpy_sub_version": mp.mpy_sub_version,
        },
        sdk={"version": sdk_info.declared_version},
        toolchain={
            "mpy_cross": {"version": mpy_cross.version, "mpy_abi_version": mp.mpy_abi_version},
            "vela": {"version": vela.version, "found": vela.found},
            "stedgeai": {"version": stedgeai.version, "found": stedgeai.found},
        },
        submodules=resolve_submodules(repo),
        targets={"boards": list(config.boards), "resolved": resolved},
    )
    return lock, warnings


def ensure_sdk(repo: Path, override: Path | None, install_sdk: bool) -> sdk_res.SdkInfo:
    """Verify the SDK is installed and matches; optionally download + install it.

    The install is pure Python (download + verify + extract; see
    :mod:`openmv_ota.project.sdk_install`), so it needs no ``make`` -- which matters
    because the SDK it installs is what *provides* ``make`` for the firmware build."""
    info = sdk_res.resolve_sdk(repo, override)
    if info.installed and info.stamp_matches:
        return info
    if not install_sdk:
        if not info.installed:
            raise ProjectError(
                "OpenMV SDK %s not installed at %s; pass --install-sdk to download it "
                "(or --sdk-home to point at an existing install)."
                % (info.declared_version, info.home)
            )
        raise ProjectError(
            "OpenMV SDK at %s is version %s but the firmware wants %s; reinstall "
            "or pass --install-sdk." % (info.home, info.stamp_version, info.declared_version)
        )
    sdk_install.install_sdk(info.declared_version, info.home)
    info = sdk_res.resolve_sdk(repo, override)
    if not (info.installed and info.stamp_matches):
        raise ProjectError("SDK still not correct at %s after install" % info.home, exit_code=1)
    return info


# Per-board override keys that are pure identity (not firmware-resolved geometry):
# editing them must not invalidate the lock, so they're excluded from the digest and
# read straight from the config by the build.
_IDENTITY_OVERRIDE_KEYS = ("product_id", "board_name")


def _ensure_ota_capable(lock: lock_mod.Lock) -> None:
    """Raise if any resolved target's ROMFS partition is too small to host OTA — i.e.
    a slot has no room for a body after its status + trailer sectors. This is the
    case for boards whose ROMFS is a single large internal-flash sector (the erase
    block is the whole partition), so the math itself proves OTA is impossible."""
    bad = [rb for rb in lock.targets.get("resolved", [])
           if rb.get("role", "main") == "main"
           and not geometry.is_ota_capable(rb["partition_size"], rb["erase_size"])]
    if not bad:
        return
    lines = [
        "%s (partition %d): %d-byte ROMFS, %d-byte erase block -> a slot is %d bytes, "
        "below the %d-byte status+trailer overhead"
        % (rb["name"], rb["partition_index"], rb["partition_size"], rb["erase_size"],
           geometry.front_size(rb["partition_size"], rb["erase_size"]),
           geometry.slot_overhead(rb["erase_size"]))
        for rb in bad
    ]
    raise ProjectError(
        "not OTA-capable: the ROMFS partition can't be split into two updatable "
        "slots:\n  - " + "\n  - ".join(lines)
        + "\nThis board keeps its ROMFS in a single large flash sector; build without "
        "--ota (a single image that fills the partition).", exit_code=1)


def _ensure_ota_mbedtls(lock: lock_mod.Lock) -> None:
    """Raise if any target board's firmware is built without mbedtls. The device OTA
    ``boot.py`` verifies image signatures with an mbedtls-backed C module, so a board
    that doesn't compile mbedtls can't run OTA -- e.g. the QEMU/FVP emulator boards
    (MPS2/MPS3), which build with ``MICROPY_SSL_MBEDTLS = 0``."""
    bad = sorted({rb["name"] for rb in lock.targets.get("resolved", [])
                  if not rb.get("mbedtls", True)})
    if not bad:
        return
    raise ProjectError(
        "not OTA-capable: %s build firmware without mbedtls, which the OTA boot.py "
        "needs to verify image signatures on-device. Build without --ota (a single "
        "image that fills the partition)." % ", ".join(bad), exit_code=1)


def _digest(config: OtaConfig) -> str:
    """Digest the *firmware-relevant* config — the fields that, if changed, would
    invalidate the resolved lock. Excludes release/identity state (``version``,
    ``product_id``, ``board_name``), metadata (name/vendor), and cosmetic edits, so
    setting a product id or bumping the version never trips drift."""
    geometry = {
        board: {k: v for k, v in ov.items() if k not in _IDENTITY_OVERRIDE_KEYS}
        for board, ov in config.overrides.items()
    }
    geometry = {board: ov for board, ov in geometry.items() if ov}  # drop now-empty entries
    relevant = {"boards": config.boards, "overrides": geometry, "ota": config.ota}
    blob = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --- create -----------------------------------------------------------------

def create_project(
    root: Path,
    *,
    firmware: Path,
    boards: list[str],
    product: str | None,
    vendor: str | None,
    sdk_home_override: Path | None,
    install_sdk: bool,
    allow_dirty: bool,
    force: bool,
    now: str,
    ota: bool = False,
    sig_alg: int = ES256,
    ota_keys: int = 32,
    factory_keys: int = 8,
    app_version: str = "1.0.0",
) -> tuple[lock_mod.Lock, list[str]]:
    repo = firmware.expanduser().resolve()
    if not gitrepo.is_git_repo(repo):
        raise ProjectError("not a git repository: %s" % repo)

    paths = ProjectPaths(root)
    root.mkdir(parents=True, exist_ok=True)
    if paths.config.exists() and not force:
        raise ProjectError("%s already exists; use --force" % paths.config.name, exit_code=1)

    config_mod.validate_boards(boards)
    name = product or root.resolve().name

    ensure_sdk(repo, sdk_home_override, install_sdk)

    warnings: list[str] = []
    provisioned = None
    signing_key_id = None
    if ota:
        if force and paths.trusted_keys.exists():
            warnings.append(
                "this regenerates the signing keys; devices already in the field trust the "
                "OLD keys and will REJECT updates signed by the new ones (you'd have to "
                "re-flash them). Only do this for a fresh fleet -- back up the old keys first")
        provisioned, w = _provision_keys(sig_alg, factory_keys, ota_keys)
        signing_key_id = provisioned.signing_key_id
        warnings += w

    config_text = config_mod.render_config(
        name, vendor, boards, ota=ota, signing_key_id=signing_key_id,
    )
    # Parse the rendered text so the digest/resolve see exactly what lands on disk
    # (incl. the scaffolded per-board sections) — otherwise `verify` would see drift.
    config = config_mod.parse_config(config_text, name)
    digest = _digest(config)
    lock, w = resolve_snapshot(
        repo, config, sdk_home_override=sdk_home_override, config_digest=digest, now=now,
    )
    warnings += w
    if config.ota:
        _ensure_ota_capable(lock)  # fail before writing anything for an impossible board
        _ensure_ota_mbedtls(lock)
    if lock.firmware["dirty"] and not allow_dirty:
        warnings.append("firmware checkout is dirty; the pinned commit does not "
                        "fully capture the build. Commit or pass --allow-dirty.")

    paths.config.write_text(config_text, encoding="utf-8")
    lock_mod.write(paths.lock, lock)
    if provisioned is not None:
        _write_keys(paths, provisioned)
    _scaffold_app(paths, app_version)  # every project gets a starter app/ (OTA or not)
    if _boards_have_coprocessor(boards):  # a slaved second core (e.g. AE3's M55_HE)
        _scaffold_coprocessor(paths, app_version)
    if config.ota:  # the device OTA runtime lib (status/confirm/sync) for the app to use
        _scaffold_runtime_lib(paths, boards)
        _scaffold_device_files(paths)  # editable logger + watchdog, frozen into firmware
    _write_local(paths, repo, sdk_home_override)
    paths.gitignore.write_text(_GITIGNORE, encoding="utf-8")
    paths.readme.write_text(_readme(name), encoding="utf-8")
    return lock, warnings


OTA_KEYS_WARN_FLOOR = 4


def _provision_keys(sig_alg: int, factory_keys: int, ota_keys: int):
    """Generate the OTA project's key set (raises on a missing factory key)."""
    if factory_keys < 1:
        raise ProjectError(
            "an OTA project needs at least one factory key (--factory-keys 0 leaves no "
            "way to sign the golden image)", exit_code=1,
        )
    from openmv_ota.ota.keys import provision_key_set

    prov = provision_key_set(algorithm_for(sig_alg), n_factory=factory_keys, n_ota=ota_keys)
    warnings: list[str] = []
    if ota_keys < OTA_KEYS_WARN_FLOOR:
        warnings.append(
            "--ota-keys %d is a very small rotation pool; you can't add keys later without "
            "re-flashing firmware (the default is 32)" % ota_keys
        )
    return prov, warnings


def _write_keys(paths: ProjectPaths, provisioned) -> None:
    """Write the committed public set + the gitignored private PEMs."""
    from openmv_ota.ota.keys import write_trusted_keys

    paths.trusted_keys.parent.mkdir(parents=True, exist_ok=True)
    write_trusted_keys(paths.trusted_keys, provisioned.trusted)
    paths.private_keys_dir.mkdir(parents=True, exist_ok=True)
    role_by_id = {k.key_id: k.role for k in provisioned.trusted}
    for key_id, pem in provisioned.private_pems.items():
        pem_path = paths.private_keys_dir / ("%s-%04x.pem" % (role_by_id[key_id], key_id))
        pem_path.write_bytes(pem)


KEY_BACKUP_NAME = "keys-backup.enc"


def backup_private_keys(root: str | Path, passphrase: str) -> Path:
    """Write an encrypted backup of every private signing PEM to ``<root>/keys-backup.enc``
    (the operator then moves it off-machine). Raises ``ProjectError`` if no private keys are
    present (nothing to back up)."""
    from . import keybackup

    pem_dir = ProjectPaths(Path(root)).private_keys_dir
    pems = ({p.name: p.read_bytes() for p in sorted(pem_dir.glob("*.pem"))}
            if pem_dir.exists() else {})
    if not pems:
        raise ProjectError("no private keys in %s to back up" % pem_dir, exit_code=1)
    out = Path(root) / KEY_BACKUP_NAME
    out.write_bytes(keybackup.encrypt_keys(pems, passphrase))
    return out


def restore_private_keys(root: str | Path, blob: bytes, passphrase: str) -> list[str]:
    """Decrypt a backup ``blob`` and write its PEMs into the project's private-key dir,
    returning the restored filenames. Raises ``ProjectError`` on a wrong passphrase / corrupt
    backup (recovery fails loudly)."""
    from . import keybackup

    pem_dir = ProjectPaths(Path(root)).private_keys_dir
    pems = keybackup.decrypt_keys(blob, passphrase)
    pem_dir.mkdir(parents=True, exist_ok=True)
    for name, pem in pems.items():
        (pem_dir / name).write_bytes(pem)
    return sorted(pems)


_APP_MAIN = """\
# main.py - your OpenMV app. Replace this with your code.
# Your app's version + settings live in settings.json, readable on-device
# (and read at build time to stamp an OTA image's version).
# Put shared modules you import (e.g. helpers.py) in lib/.
import time

while True:
    time.sleep_ms(1000)
"""


def _scaffold_app(paths: ProjectPaths, app_version: str) -> None:
    """Scaffold a starter ``app/`` for any project: the user-editable settings file
    (version + vendor), a placeholder ``main.py``, and a ``lib/`` directory for the
    app's own importable modules. Useful for every project — the app reads its own
    settings on-device — and for OTA the build also reads the version from here.
    Existing files are left alone, so re-running ``new --force`` never clobbers the
    user's app."""
    paths.app_dir.mkdir(parents=True, exist_ok=True)
    if not paths.app_settings.exists():
        # rollback_floor starts equal to the version (no real constraint yet); see the
        # docs - raise it only to forbid downgrades past a critical fix, never per release.
        settings = {"app_version": app_version, "vendor": "", "rollback_floor": app_version}
        paths.app_settings.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    main_py = paths.app_dir / "main.py"
    if not main_py.exists():
        main_py.write_text(_APP_MAIN, encoding="utf-8")
    lib_dir = paths.app_dir / "lib"
    if not lib_dir.exists():
        # A starter directory for the app's own library modules. The .gitkeep keeps
        # the empty dir in git; it is excluded from packed images (DEFAULT_EXCLUDES).
        lib_dir.mkdir(parents=True)
        (lib_dir / ".gitkeep").write_text("", encoding="utf-8")


_COPROCESSOR_MAIN = """\
# main.py - the coprocessor core's app (e.g. the AE3 M55_HE helper core).
# This partition is a *plain* romfs written by the main core; it is never OTA-updated
# on its own. Put its code + models here; `openmv-ota build romfs` packs them into
# <board>-coprocessor-romfs.img automatically (no per-partition config needed).
import time

while True:
    time.sleep_ms(1000)
"""


def _boards_have_coprocessor(boards: list[str]) -> bool:
    """True if any selected board has a coprocessor (slaved second) partition."""
    return any(
        part.role == "coprocessor"
        for name in boards
        for part in boards_mod.get_board(name).partitions
    )


def _scaffold_coprocessor(paths: ProjectPaths, app_version: str) -> None:
    """Scaffold ``app-coprocessor/`` for boards with a slaved second core. It's a plain
    romfs (no OTA), so there's no rollback/version constraint -- just a starter
    ``main.py``, a ``settings.json`` (version + vendor stamped into /rom/system.json),
    and a ``lib/``. Existing files are left alone."""
    d = paths.coprocessor_app_dir
    d.mkdir(parents=True, exist_ok=True)
    settings = d / "settings.json"
    if not settings.exists():
        settings.write_text(
            json.dumps({"app_version": app_version, "vendor": ""}, indent=2) + "\n",
            encoding="utf-8")
    main_py = d / "main.py"
    if not main_py.exists():
        main_py.write_text(_COPROCESSOR_MAIN, encoding="utf-8")
    lib_dir = d / "lib"
    if not lib_dir.exists():
        lib_dir.mkdir(parents=True)
        (lib_dir / ".gitkeep").write_text("", encoding="utf-8")


# The device-side OTA runtime library source (status/confirm/sync), shipped with
# the tool and scaffolded into an OTA project's app/lib/openmv_ota/.
_RUNTIME_LIB_SRC = Path(__file__).resolve().parents[1] / "build" / "device" / "openmv_ota"

# The editable OTA logger, scaffolded to <project>/device/openmv_log.py and frozen as openmv_log
# by `build firmware` (see build/firmware.py).
# Editable device modules scaffolded into <project>/device/ and frozen into the firmware
# (the logger + the watchdog helper); see build/firmware.py.
_DEVICE_SRC_DIR = Path(__file__).resolve().parents[1] / "build" / "device"
_DEVICE_MODULES = ("openmv_log.py", "openmv_wdt.py")


def _coprocessor_partitions(boards: list[str]) -> list[dict]:
    """Distinct coprocessor partitions across the project's boards, as
    ``{"index", "name"}`` -- the targets sync() writes (deduped by index)."""
    out: list[dict] = []
    seen: set[int] = set()
    for name in boards:
        for part in boards_mod.get_board(name).partitions:
            if part.role == "coprocessor" and part.index not in seen:
                seen.add(part.index)
                out.append({"index": part.index, "name": part.name})
    return out


def _empty_romfs() -> bytes:
    """A valid, empty ROMFS image -- the placeholder coprocessor resource the build
    replaces with the real image, so the scaffolded app layout is always valid."""
    import tempfile

    from openmv_ota.romfs.builder import build_image
    with tempfile.TemporaryDirectory() as empty:
        return build_image(empty)


def _fetch_ca_bundle(url: str = CA_BUNDLE_URL) -> bytes:
    """Download the CA root bundle for the device's OTA TLS trust store. No offline
    fallback -- creating an OTA project requires network access (like the SDK)."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "openmv-ota"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except (urllib.error.URLError, OSError) as e:
        raise ProjectError(
            "could not download the CA bundle from %s (%s); creating an OTA project "
            "needs network access" % (url, e), exit_code=1) from None
    if b"BEGIN CERTIFICATE" not in data:
        raise ProjectError("downloaded CA bundle from %s looks invalid" % url, exit_code=1)
    return data


def _scaffold_runtime_lib(paths: ProjectPaths, boards: list[str]) -> None:
    """Scaffold ``app/lib/openmv_ota/`` -- the device OTA runtime helpers
    (status/confirm/sync/install) -- into an OTA project. ``data/`` always gets the
    installer source (shipped uncompiled so ``install()`` can ``exec`` it into RAM) and
    a freshly-downloaded ``ca.pem`` (the TLS trust store). For coprocessor boards it
    also seeds ``data/coprocessor.romfs`` (a placeholder the build swaps for the real
    image) and ``data/resources.json`` (the sync() manifest). Existing files are left
    alone, so a user's replaced ``ca.pem`` survives ``new --force``."""
    dst = paths.app_dir / "lib" / "openmv_ota"
    dst.mkdir(parents=True, exist_ok=True)
    for src in sorted(_RUNTIME_LIB_SRC.glob("*.py")):
        out = dst / src.name
        if not out.exists():
            out.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    data = dst / "data"
    data.mkdir(exist_ok=True)
    installer = data / "installer.py"
    if not installer.exists():
        installer.write_text(
            (_RUNTIME_LIB_SRC / "data" / "installer.py").read_text(encoding="utf-8"),
            encoding="utf-8")
    ca = data / "ca.pem"
    if not ca.exists():
        ca.write_bytes(_fetch_ca_bundle())

    copro = _coprocessor_partitions(boards)
    if copro:
        romfs = data / "coprocessor.romfs"
        if not romfs.exists():
            romfs.write_bytes(_empty_romfs())
        manifest = data / "resources.json"
        if not manifest.exists():
            entries = [{"file": "coprocessor.romfs", "handler": "partition",
                        "partition": p["index"], "name": p["name"]} for p in copro]
            manifest.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")


def _scaffold_device_files(paths: ProjectPaths) -> None:
    """Scaffold the editable device modules frozen into the firmware -- the logger
    (``openmv_log``) and the watchdog helper (``openmv_wdt``), both shared by the
    installer and your app and both off until you edit + rebuild. Left alone if present."""
    d = paths.root / "device"
    d.mkdir(parents=True, exist_ok=True)
    for name in _DEVICE_MODULES:
        out = d / name
        if not out.exists():
            out.write_text((_DEVICE_SRC_DIR / name).read_text(encoding="utf-8"),
                           encoding="utf-8")


def _write_local(paths: ProjectPaths, repo: Path, sdk_home_override: Path | None) -> None:
    paths.local.write_text(config_mod.render_local(repo, sdk_home_override), encoding="utf-8")


def _readme(name: str) -> str:
    return (
        "# %s\n\n"
        "An OpenMV OTA project, pegged to a specific OpenMV firmware commit.\n\n"
        "- `openmv-ota.toml` / `openmv-ota.lock.json` are committed.\n"
        "- `openmv-ota.local.toml` (this machine's firmware path) is gitignored.\n\n"
        "After cloning, run `openmv-ota project setup` to reconstruct the pinned\n"
        "firmware checkout and SDK on your machine.\n" % name
    )


# --- sync -------------------------------------------------------------------

def sync_project(
    root: Path,
    *,
    firmware: Path | None,
    sdk_home_override: Path | None,
    install_sdk: bool,
    allow_dirty: bool,
    now: str,
) -> tuple[lock_mod.Lock, list[str]]:
    paths = ProjectPaths(root)
    config = config_mod.load_config(paths.config)
    repo = _checkout_path(paths, firmware)
    ensure_sdk(repo, sdk_home_override, install_sdk)

    lock, warnings = resolve_snapshot(
        repo, config, sdk_home_override=sdk_home_override,
        config_digest=_digest(config), now=now,
    )
    if config.ota:
        _ensure_ota_capable(lock)
        _ensure_ota_mbedtls(lock)
    if lock.firmware["dirty"] and not allow_dirty:
        warnings.append("firmware checkout is dirty; re-locked anyway.")
    lock_mod.write(paths.lock, lock)
    return lock, warnings


def _current_snapshot(paths: ProjectPaths, firmware: Path | None, now: str = "") -> lock_mod.Lock:
    """Re-resolve the snapshot from the current checkout (``now`` is irrelevant —
    ``generated_at`` is excluded from drift comparison)."""
    config = config_mod.load_config(paths.config)
    repo = _checkout_path(paths, firmware)
    current, _ = resolve_snapshot(
        repo, config, sdk_home_override=_local_sdk_home(paths),
        config_digest=_digest(config), now=now,
    )
    return current


def status_project(root: Path, *, firmware: Path | None, now: str = "") -> list[str]:
    """Return drift descriptions (empty == in sync)."""
    paths = ProjectPaths(root)
    locked = lock_mod.read(paths.lock)
    return lock_mod.drift(locked, _current_snapshot(paths, firmware, now))


def verify_locked(root: Path, *, firmware: Path | None = None) -> list[str]:
    """Return reasons the firmware no longer matches the lock (empty == verified).

    Stricter than ``status``: any drift from the lock **and** a dirty working tree
    are reported, because uncommitted changes are not captured by the pinned
    commit. This is the guarantee that nothing has changed since the project was
    pegged — the gate upper layers run before building images.
    """
    paths = ProjectPaths(root)
    locked = lock_mod.read(paths.lock)
    current = _current_snapshot(paths, firmware)
    problems = lock_mod.drift(locked, current)
    if current.firmware["dirty"]:
        problems.insert(0, "firmware checkout is dirty; uncommitted changes are "
                           "not captured by the pinned commit")
    return problems


def _checkout_path(paths: ProjectPaths, firmware: Path | None) -> Path:
    if firmware is not None:
        return firmware.expanduser().resolve()
    local = config_mod.load_local(paths.local)
    if local is None:
        raise ProjectError(
            "no firmware checkout: pass -f/--firmware or run `openmv-ota project setup`"
        )
    return local.firmware_path.expanduser().resolve()


def _local_sdk_home(paths: ProjectPaths) -> Path | None:
    local = config_mod.load_local(paths.local)
    return local.sdk_home if local else None


# --- setup ------------------------------------------------------------------

def setup_project(
    root: Path,
    *,
    cache_override: str | None,
    sdk_home_override: Path | None,
    install_sdk: bool,
) -> Path:
    """Reconstruct the pinned checkout + SDK from the committed lock; write the
    local file. Returns the firmware checkout path."""
    paths = ProjectPaths(root)
    locked = lock_mod.read(paths.lock)
    fw = locked.firmware
    remote, commit = fw.get("remote"), fw.get("commit")
    if not remote or not commit:
        raise ProjectError("lock has no firmware remote/commit to clone from")

    dest = cache.firmware_clone_dir(commit, cache_override)
    if not gitrepo.is_git_repo(dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        gitrepo.clone(remote, dest, commit)
    gitrepo.submodule_update(dest)

    if install_sdk:
        ensure_sdk(dest, sdk_home_override, install_sdk=True)
        _ensure_mpy_cross(locked.micropython.get("version"))
    _write_local(paths, dest, sdk_home_override)
    return dest


def _mpy_cross_installed() -> bool:
    return importlib.util.find_spec("mpy_cross") is not None


def _ensure_mpy_cross(version: str | None) -> None:
    """Best-effort: pip-install the matching mpy-cross unless it is already present.

    A failure (e.g. that version not on PyPI) only warns — the user can build the
    firmware to get mpy-cross, or install it manually.
    """
    if not version or _mpy_cross_installed():
        return
    try:
        gitrepo.pip_install("mpy-cross==%s" % version)
    except ProjectError as e:
        print("warning: %s; build the firmware or `pip install mpy-cross==%s` to "
              "compile .py files" % (e, version), file=sys.stderr)


# --- load API (the contract upper layers consume) ---------------------------

@dataclass
class LoadedProject:
    root: Path
    config: OtaConfig
    lock: lock_mod.Lock
    firmware_path: Path
    sdk_home: Path

    @property
    def mpy_cross_path(self) -> str | None:
        mp = mp_res.resolve_micropython(self.firmware_path)
        return sdk_res.resolve_mpy_cross(self.firmware_path, mp).path

    @property
    def vela_path(self) -> str | None:
        return sdk_res.resolve_vela(self.sdk_home).path

    @property
    def stedgeai_path(self) -> str | None:
        return sdk_res.resolve_stedgeai(self.sdk_home).path

    @property
    def targets(self) -> list[ResolvedBoard]:
        """Every (board, partition) target, for the build layer to iterate."""
        return [ResolvedBoard(**e) for e in self.lock.targets.get("resolved", [])]

    def board(self, name: str, partition: int = 0) -> ResolvedBoard:
        for entry in self.lock.targets.get("resolved", []):
            if entry["name"] == name and entry["partition_index"] == partition:
                return ResolvedBoard(**entry)
        raise ProjectError(
            "board %r partition %d is not a target of this project" % (name, partition)
        )


def load_project(
    root: str | Path,
    firmware: str | Path | None = None,
    *,
    verify: bool = True,
) -> LoadedProject:
    """Load a project for downstream layers (model compile, build romfs, …).

    By default this **verifies** that the firmware checkout still matches the lock
    and is clean, refusing to load otherwise — so a build can never run against a
    drifted firmware. Pass ``verify=False`` to load without that check (reserved
    for the future firmware-update path).
    """
    paths = ProjectPaths(Path(root))
    fw = Path(firmware) if firmware is not None else None
    if verify:
        problems = verify_locked(paths.root, firmware=fw)
        if problems:
            raise ProjectError(
                "firmware no longer matches the lock; refusing to proceed:\n  - "
                + "\n  - ".join(problems)
                + "\nRun `openmv-ota project status` to inspect, or "
                "`openmv-ota project sync` to re-peg."
            )
    config = config_mod.load_config(paths.config)
    locked = lock_mod.read(paths.lock)
    repo = _checkout_path(paths, fw)

    sdk_home = _local_sdk_home(paths)
    if sdk_home is None:
        sdk_home = sdk_res.default_sdk_home(sdk_res.read_sdk_version(repo))
    return LoadedProject(
        root=paths.root, config=config, lock=locked,
        firmware_path=repo, sdk_home=sdk_home,
    )
