"""Project orchestration: create, load, resolve the snapshot, diff, and setup."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from openmv_ota import __version__

from . import cache, config as config_mod, gitrepo, lock as lock_mod
from .config import LOCAL_NAME, OtaConfig
from .errors import ProjectError
from .resolve import firmware as fw_res
from .resolve import micropython as mp_res
from .resolve import sdk as sdk_res
from .resolve.board import ResolvedBoard, resolve_board
from .resolve.submodules import resolve_submodules

GENERATED_BY = "openmv-ota %s" % __version__

_GITIGNORE = """\
# openmv-ota: machine-local settings (never commit)
openmv-ota.local.toml

# private signing material
keys/*.pem
keys/*.key
keys/private/

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
        parts = override.get("partitions")
        if parts is None:
            parts = [override.get("partition", 0)]
        elif not isinstance(parts, list) or not all(isinstance(i, int) for i in parts):
            raise ProjectError("[targets.%s].partitions must be a list of integers" % name)
        if len(parts) > 1 and "partition_size" in override:
            raise ProjectError(
                "[targets.%s]: partition_size cannot be set when targeting multiple "
                "partitions" % name
            )
        for idx in parts:
            per = {k: v for k, v in override.items() if k in ("board_id", "partition_size")}
            rb, w = resolve_board(repo, name, int(idx), per)
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
    """Verify the SDK is installed and matches; optionally run ``make sdk``."""
    info = sdk_res.resolve_sdk(repo, override)
    if info.installed and info.stamp_matches:
        return info
    if not install_sdk:
        if not info.installed:
            raise ProjectError(
                "OpenMV SDK %s not installed at %s; run `make sdk` in the firmware "
                "repo, or pass --install-sdk (or --sdk-home)."
                % (info.declared_version, info.home)
            )
        raise ProjectError(
            "OpenMV SDK at %s is version %s but the firmware wants %s; reinstall "
            "or pass --install-sdk." % (info.home, info.stamp_version, info.declared_version)
        )
    gitrepo.run_make_sdk(repo)
    info = sdk_res.resolve_sdk(repo, override)
    if not (info.installed and info.stamp_matches):
        raise ProjectError("SDK still not correct at %s after `make sdk`" % info.home, exit_code=1)
    return info


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    config = OtaConfig(name=name, vendor=vendor, boards=boards, ota=ota, overrides={})

    ensure_sdk(repo, sdk_home_override, install_sdk)

    config_text = config_mod.render_config(name, vendor, boards, ota=ota)
    digest = _digest(config_text)
    lock, warnings = resolve_snapshot(
        repo, config, sdk_home_override=sdk_home_override, config_digest=digest, now=now,
    )
    if lock.firmware["dirty"] and not allow_dirty:
        warnings.append("firmware checkout is dirty; the pinned commit does not "
                        "fully capture the build. Commit or pass --allow-dirty.")

    paths.config.write_text(config_text, encoding="utf-8")
    lock_mod.write(paths.lock, lock)
    _write_local(paths, repo, sdk_home_override)
    paths.gitignore.write_text(_GITIGNORE, encoding="utf-8")
    paths.readme.write_text(_readme(name), encoding="utf-8")
    return lock, warnings


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

    config_text = paths.config.read_text(encoding="utf-8")
    lock, warnings = resolve_snapshot(
        repo, config, sdk_home_override=sdk_home_override,
        config_digest=_digest(config_text), now=now,
    )
    if lock.firmware["dirty"] and not allow_dirty:
        warnings.append("firmware checkout is dirty; re-locked anyway.")
    lock_mod.write(paths.lock, lock)
    return lock, warnings


def _current_snapshot(paths: ProjectPaths, firmware: Path | None, now: str = "") -> lock_mod.Lock:
    """Re-resolve the snapshot from the current checkout (``now`` is irrelevant —
    ``generated_at`` is excluded from drift comparison)."""
    config = config_mod.load_config(paths.config)
    repo = _checkout_path(paths, firmware)
    config_text = paths.config.read_text(encoding="utf-8")
    current, _ = resolve_snapshot(
        repo, config, sdk_home_override=_local_sdk_home(paths),
        config_digest=_digest(config_text), now=now,
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
