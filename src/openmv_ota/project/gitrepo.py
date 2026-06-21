"""Thin ``git`` porcelain wrapper — the only module that shells out.

Keeping every subprocess call here (plus the ``make sdk`` seam) means the rest of
the package is pure file parsing, and tests can drive everything from real temp
repos while mocking only the heavy clone / build seams.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import ProjectError

# Binary names, overridable for tests.
GIT = "git"
MAKE = "make"


def run_git(repo: Path, *args: str, check: bool = True) -> str | None:
    """Run ``git -C <repo> <args>`` and return stripped stdout.

    With ``check`` (default), a non-zero exit raises :class:`ProjectError`;
    without it, a non-zero exit returns ``None``. A missing ``git`` binary always
    raises.
    """
    try:
        proc = subprocess.run(
            [GIT, "-C", str(repo), *args],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        raise ProjectError("git not found on PATH") from None
    if proc.returncode != 0:
        if check:
            raise ProjectError(
                "git %s failed: %s" % (" ".join(args), proc.stderr.strip())
            )
        return None
    return proc.stdout.strip()


def is_git_repo(path: Path) -> bool:
    try:
        return run_git(path, "rev-parse", "--git-dir", check=False) is not None
    except ProjectError:
        return False


def head_commit(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD")


def current_branch(repo: Path) -> str | None:
    branch = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return None if branch == "HEAD" else branch


def describe(repo: Path) -> str:
    return run_git(repo, "describe", "--tags", "--always", "--dirty")


def is_dirty(repo: Path) -> bool:
    return bool(run_git(repo, "status", "--porcelain"))


def remote_url(repo: Path) -> str | None:
    return run_git(repo, "remote", "get-url", "origin", check=False)


def submodule_status(repo: Path) -> list[dict]:
    """Parse ``git submodule status``.

    Each line is ``<flag><sha> <path> (<describe>)`` where flag is ``-``
    (uninitialized), ``+`` (checked-out commit differs), or a space.
    """
    out = run_git(repo, "submodule", "status") or ""
    entries: list[dict] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        flag = line[0]
        rest = line[1:].split()
        if len(rest) < 2:
            continue
        commit, path = rest[0], rest[1]
        describe_str = None
        if len(rest) >= 3:
            describe_str = rest[2].strip("()")
        entries.append({
            "path": path,
            "commit": commit,
            "describe": describe_str,
            "initialized": flag != "-",
        })
    return entries


# --- mutating seams (mocked in tests) ---------------------------------------

def clone(remote: str, dest: Path, commit: str | None = None) -> None:
    try:
        subprocess.run([GIT, "clone", remote, str(dest)], check=True)
        if commit:
            subprocess.run([GIT, "-C", str(dest), "checkout", commit], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise ProjectError("git clone failed: %s" % e, exit_code=1) from None


def submodule_update(repo: Path) -> None:
    try:
        subprocess.run(
            [GIT, "-C", str(repo), "submodule", "update", "--init", "--recursive"],
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise ProjectError("git submodule update failed: %s" % e, exit_code=1) from None


def run_make_sdk(repo: Path) -> None:
    try:
        subprocess.run([MAKE, "sdk"], cwd=str(repo), check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise ProjectError("make sdk failed: %s" % e, exit_code=1) from None
