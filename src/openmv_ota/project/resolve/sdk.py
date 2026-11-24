"""Resolve the OpenMV SDK and the model-compilation toolchain.

The SDK version pins the toolchain: ``make sdk`` installs a prebuilt bundle to
``~/openmv-sdk-<SDK_VERSION>``. Tool *versions* are read from files (the bundled
python's ``dist-info`` directory for vela, the ``stedgeai<MMmm>`` directory name
for ST Edge AI, the MicroPython source for mpy-cross). Tool *binary paths* are
best-effort filesystem lookups for this machine — they are exposed by the load
API but never written to the committed lock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import gitrepo
from ..errors import ProjectError
from .micropython import MICROPYTHON_SUBPATH, MicroPythonInfo

SDK_VERSION_FILE = "SDK_VERSION"
SDK_STAMP_FILE = "sdk.version"


@dataclass(frozen=True)
class ToolInfo:
    version: str | None
    found: bool
    path: str | None  # absolute posix; machine-local, not committed


@dataclass(frozen=True)
class SdkInfo:
    declared_version: str
    home: Path
    installed: bool
    stamp_version: str | None
    stamp_matches: bool


def read_sdk_version(repo: Path) -> str:
    f = repo / SDK_VERSION_FILE
    try:
        # The tree may be one someone else made. As a link this file is any file of the
        # user's, and what it holds is printed (and names a URL and a directory): a version
        # and nothing else, and never quoted back when it is not one.
        value = "" if f.is_symlink() else f.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        raise ProjectError("%s not found at %s" % (SDK_VERSION_FILE, f)) from None
    if not value and not f.is_symlink():
        raise ProjectError("%s is empty" % SDK_VERSION_FILE)
    if not re.fullmatch(r"[0-9]+(\.[0-9A-Za-z]+){1,3}", value):
        raise ProjectError("%s at %s does not hold a version" % (SDK_VERSION_FILE, f))
    return value


def default_sdk_home(version: str) -> Path:
    return Path.home() / ("openmv-sdk-%s" % version)


def resolve_sdk(repo: Path, sdk_home_override: Path | None) -> SdkInfo:
    declared = read_sdk_version(repo)
    home = sdk_home_override if sdk_home_override is not None else default_sdk_home(declared)
    stamp = home / SDK_STAMP_FILE
    if stamp.exists():
        stamp_version = stamp.read_text(encoding="utf-8").strip()
        installed = True
    else:
        stamp_version = None
        installed = False
    return SdkInfo(
        declared_version=declared,
        home=home,
        installed=installed,
        stamp_version=stamp_version,
        stamp_matches=installed and stamp_version == declared,
    )


def _first_existing(candidates: list[Path]) -> str | None:
    for c in candidates:
        if c.exists():
            return c.as_posix()
    return None


def resolve_vela(home: Path) -> ToolInfo:
    matches = sorted(home.glob("python/**/site-packages/ethos_u_vela-*.dist-info"))
    if not matches:
        return ToolInfo(version=None, found=False, path=None)
    name = matches[-1].name  # ethos_u_vela-<ver>.dist-info
    version = name[len("ethos_u_vela-"):-len(".dist-info")]
    path = _first_existing([
        home / "python" / "bin" / "vela",
        home / "python" / "bin" / "vela.exe",
        home / "python" / "Scripts" / "vela.exe",
        home / "python" / "Scripts" / "vela",
    ])
    return ToolInfo(version=version, found=True, path=path)


def resolve_stedgeai(home: Path) -> ToolInfo:
    best: tuple[int, int] | None = None
    best_dir: Path | None = None
    for d in sorted((home / "stedgeai").glob("stedgeai*")):
        m = re.fullmatch(r"stedgeai(\d{2})(\d{2})", d.name)
        if not m:
            continue
        ver = (int(m.group(1)), int(m.group(2)))
        if best is None or ver > best:
            best, best_dir = ver, d
    if best is None:
        return ToolInfo(version=None, found=False, path=None)
    version = "%d.%d" % best
    path = _first_existing([
        best_dir / "Utilities" / plat / name
        for plat in ("linux", "macarm", "windows")
        for name in ("stedgeai", "stedgeai.exe")
    ])
    return ToolInfo(version=version, found=True, path=path)


def resolve_mpy_cross(repo: Path, mp: MicroPythonInfo) -> ToolInfo:
    # Version derives from the MicroPython source; no binary is executed.
    base = repo / MICROPYTHON_SUBPATH / "mpy-cross"
    path = _first_existing([
        base / "build" / "mpy-cross",
        base / "build" / "mpy-cross.exe",
    ])
    if path is not None and (Path(path).is_symlink() or gitrepo.run_git(
            Path(path).parent, "ls-files", "--error-unmatch", "--", Path(path).name,
            check=False) is not None):
        # A binary the checkout CAME with is not one this machine built: a fork that commits
        # a program at this path had `build romfs` run it. The built one is untracked. (Asked
        # of the directory it is in: MicroPython is a submodule, with its own index.)
        path = None
    return ToolInfo(version=mp.version, found=True, path=path)
