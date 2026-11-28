"""Thin ``git`` porcelain wrapper — the only module that shells out.

Keeping every subprocess call here (plus the ``make sdk`` seam) means the rest of
the package is pure file parsing, and tests can drive everything from real temp
repos while mocking only the heavy clone / build seams.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .errors import ProjectError

# Binary names, overridable for tests.
GIT = "git"

# The tree is one the project was pointed at, and its own config can name a program for git
# to run (a filesystem monitor on any read of the index, hooks): read, never run. Nor may its
# config narrow what git looks at: a replaced object (`git replace` has the pinned commit read
# as another, with the other's files), untracked files left unlisted, or so little of a file's
# recorded stat checked that a changed file of the same size and mtime (an archive keeps both)
# is taken as unchanged without being read. These are `-c` settings because git hands those
# down to the git it runs in a submodule.
_QUIET = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
          "-c", "core.useReplaceRefs=false", "-c", "status.showUntrackedFiles=all",
          "-c", "core.checkStat=default")
# ...and none of these commands fetches. A tree set up as a partial clone has git fetch a
# missing object on any read, with the transport program its config names for the remote.
# (The git a submodule's status runs drops GIT_NO_REPLACE_OBJECTS; core.useReplaceRefs is
# what reaches it.)
_ENV = {"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_NO_REPLACE_OBJECTS": "1"}


def _env_count() -> int:
    """How many config settings the caller's environment carries for git (0 when the
    count is not a number: git itself stops on that, and this replaces it)."""
    try:
        return max(0, int(os.environ.get("GIT_CONFIG_COUNT", "0")))
    except ValueError:
        return 0


def _filter_names(repo: Path, nested: bool, seen: set[str]) -> None:
    """Collect the content filters a tree's own config defines -- the files it includes and
    a worktree's config too -- and, with ``nested``, those of each submodule checked out
    under it (`git status` looks into them with their own config). Reading runs nothing.

    The tree git works on is the one this walks: a config that moves the worktree somewhere
    else, or a submodule's path that is a link to another repository, would have git read
    files this never looked at, and such a tree is refused."""
    def git(*args: str) -> list[str]:
        proc = subprocess.run([GIT, *_QUIET, "-C", str(repo), *args], capture_output=True,
                              text=True, errors="surrogateescape", env=env)
        return proc.stdout.split("\0") if proc.returncode == 0 else []
    env = {**os.environ, **_ENV, "GIT_CONFIG_COUNT": str(_env_count())}
    fields = git("config", "--includes", "--show-scope", "--name-only", "-z",
                 "--get-regexp", r"^filter\.")
    for scope, name in zip(fields[0::2], fields[1::2], strict=False):
        # the user's own filters (git-lfs) stay: without one a tree that uses it reads dirty
        if scope in ("local", "worktree") and name.count(".") >= 2:
            seen.add(name.rsplit(".", 1)[0])
    if not nested:
        return
    top = (git("rev-parse", "--show-toplevel") or [""])[0].rstrip("\n")
    if not top:
        return                  # not a work tree: git has no files to read here either
    here = Path(os.path.realpath(repo))
    # ...and the same repository from there: a worktree moved to a directory above has
    # git list another index (or none) from it, while it reads this one's submodules
    gitdir = [(subprocess.run([GIT, *_QUIET, "-C", str(d), "rev-parse", "--absolute-git-dir"],
                              capture_output=True, text=True, errors="surrogateescape", env=env).stdout.strip())
              for d in (repo, top)]
    if (Path(top) != here and Path(top) not in here.parents) or gitdir[0] != gitdir[1]:
        raise ProjectError("%s: its git config works on another tree (%s)" % (repo, top))
    for entry in subprocess.run([GIT, *_QUIET, "-C", top, "ls-files", "-s", "-z"],
                                capture_output=True, text=True, errors="surrogateescape", env=env).stdout.split("\0"):
        mode, _, path = entry.partition("\t")
        if not mode.startswith("160000"):
            continue
        sub = Path(top) / path
        if sub.is_symlink():
            raise ProjectError("%s: the submodule %s is a link" % (repo, path))
        if (sub / ".git").exists():
            _filter_names(sub, True, seen)


def _no_filters(repo: Path, nested: bool = False) -> dict[str, str]:
    """Environment that empties every content filter the tree's own config defines (`git
    status` runs a filter's `clean` program on a changed file). It is the environment and
    not `-c`: a filter's name may hold an `=`, which `-c` would read as the end of the key,
    and git hands it down to the git it runs in a submodule. It adds to any config the
    caller's own environment already carries this way."""
    names: set[str] = set()
    try:
        _filter_names(repo, nested, names)
    except FileNotFoundError:
        return {}           # run_git reports the missing binary
    pairs = [(name + key, value) for name in sorted(names) for key, value in
             ((".clean", ""), (".smudge", ""), (".process", ""), (".required", "false"))]
    base = _env_count()
    env = {**_ENV, "GIT_CONFIG_COUNT": str(base + len(pairs))}
    for i, (key, value) in enumerate(pairs, base):
        env["GIT_CONFIG_KEY_%d" % i] = key
        env["GIT_CONFIG_VALUE_%d" % i] = value
    return env


def run_git(repo: Path, *args: str, check: bool = True) -> str | None:
    """Run ``git -C <repo> <args>`` and return stripped stdout.

    With ``check`` (default), a non-zero exit raises :class:`ProjectError`;
    without it, a non-zero exit returns ``None``. A missing ``git`` binary always
    raises.
    """
    try:
        proc = subprocess.run(
            [GIT, *_QUIET, "-C", str(repo), *args],
            capture_output=True, text=True, errors="surrogateescape",
            # only these two read the files of the tree, and of its submodules
            env={**os.environ, **_no_filters(repo, args[:1] in (("status",), ("describe",)))},
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


def untracked(path: Path) -> bool | None:
    """Whether git knows ``path`` as a file it does NOT track: True only when the repository
    the file is in says so, False when it tracks it, None when git could not say (no
    repository there, one it refuses to read)."""
    try:
        proc = subprocess.run([GIT, *_QUIET, "-C", str(path.parent), "ls-files",
                               "--error-unmatch", "--", path.name],
                              capture_output=True, text=True, errors="surrogateescape", env={**os.environ, **_ENV})
    except FileNotFoundError:
        return None
    if proc.returncode == 0:
        return False
    return True if proc.returncode == 1 and "did not match" in proc.stderr else None


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
    # every untracked file, and every change in a submodule whatever the tree's config or
    # .gitmodules says to ignore of it
    return bool(run_git(repo, "status", "--porcelain", "--untracked-files=all",
                        "--ignore-submodules=none"))


def remote_url(repo: Path) -> str | None:
    return run_git(repo, "remote", "get-url", "origin", check=False)


# What a firmware build leaves in the tree for git to ignore: make's output, the host mpy-cross
# it builds, and Python's byte-code caches. Any other file git ignores is one the tree hides
# from `git status`: a pattern in its own config (`info/exclude`), or in a `.gitignore` it does
# not track, keeps a new source file or makefile include out of the list.
_BUILD_OUTPUT = ("build/", "lib/micropython/mpy-cross/build/")


def dirty_paths(repo: Path, limit: int = 5) -> list[str]:
    """What keeps ``repo`` from being the commits it is pinned to, in it and in each submodule
    checked out under it, as the first few -- so a dirty-checkout refusal can NAME the
    offenders instead of sending the user spelunking. That is what `git status` reports, a
    file git ignores that no build left, a file the index tells git not to look at
    (assume-unchanged, skip-worktree) and a replaced object (`git replace`). Empty is clean.

    Each repository is asked itself, and asked of its submodules only whether they moved: the
    git `status` runs in a submodule reads that submodule's config, which can have it ignore
    the submodules nested in it."""
    env = {**os.environ, **_no_filters(repo, True)}
    found: list[str] = []
    todo = [(repo, "")]
    try:
        while todo:
            here, pre = todo.pop()

            def git(*args: str, here: Path = here) -> str:
                proc = subprocess.run([GIT, *_QUIET, "-C", str(here), *args],
                                      capture_output=True, text=True, errors="surrogateescape", env=env)
                return proc.stdout if proc.returncode == 0 else ""
            fields = iter(git("status", "--porcelain", "-z", "--untracked-files=all",
                              "--ignored=matching", "--ignore-submodules=dirty").split("\0"))
            for entry in fields:
                if {"R", "C"} & set(entry[:2]):
                    next(fields, "")                  # a rename's old name
                path = pre + entry[3:]
                # (with every untracked file listed, git names each ignored file in a cache,
                # not the directory: `tools/__pycache__/haar2c.cpython-312.pyc`)
                if entry and not (entry[:2] == "!!" and (path in _BUILD_OUTPUT or
                                                         "/__pycache__/" in "/" + path)):
                    found.append(entry[:3].strip() + " " + path)
            found += ["%s%s (git is told not to look at it)" % (pre, e[2:])
                      for e in git("ls-files", "-v", "-z").split("\0")
                      if e[:1].islower() or e[:1] == "S"]
            found += ["%s%s (a replaced object)" % (pre, ref)
                      for ref in git("for-each-ref", "--format=%(refname)", "refs/replace/").split()]
            for entry in git("ls-files", "-s", "-z").split("\0"):
                mode, _, path = entry.partition("\t")
                if mode.startswith("160000") and (here / path / ".git").exists():
                    todo.append((here / path, pre + path + "/"))
    except FileNotFoundError:
        raise ProjectError("git not found on PATH") from None
    return found[:limit] + (["... and %d more" % (len(found) - limit)]
                            if len(found) > limit else [])


def submodule_remotes(repo: Path) -> dict[str, str]:
    """``{path: url}`` from ``.gitmodules`` (empty when there are none). The remote is a
    submodule's upstream identity -- what the SBOM needs to give it a purl."""
    out = run_git(repo, "config", "--file", ".gitmodules",
                  "--get-regexp", r"submodule\..*\.(path|url)", check=False) or ""
    paths: dict[str, str] = {}
    urls: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        name, _, attr = key.rpartition(".")
        if not value:
            continue
        if attr == "path":
            paths[name] = value
        elif attr == "url":
            urls[name] = value
    return {path: urls[name] for name, path in paths.items() if name in urls}


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
        # run_git strips the whole output, which takes the FIRST line's leading-space flag with
        # it: reading line[0] as the flag there ate the first character of the first
        # submodule's sha (lib/apriltag's, in the lock and the SBOM). The flag is optional.
        flag = line[0] if line[0] in " +-U" else " "
        rest = (line[1:] if line[0] in " +-U" else line).split()
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
    # Both come from a lock file someone else may have written. A commit is a full hash, and
    # `--` ends the options: a remote that starts with a dash is a path, never a flag.
    if commit is not None and not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", commit):
        raise ProjectError("not a commit hash: %r" % commit, exit_code=1)
    # A clone fetches, over the transports a firmware remote is (a local mirror too): an
    # `ext::` or remote-helper URL in the lock runs a program. Everything else is as for the
    # reads above.
    env = {**os.environ, **_ENV, "GIT_ALLOW_PROTOCOL": "https:ssh:file"}
    try:
        subprocess.run([GIT, *_QUIET, "clone", "--", remote, str(dest)], check=True, env=env)
        if commit:
            subprocess.run([GIT, *_QUIET, "-C", str(dest), "checkout", commit], check=True,
                           env={**os.environ, **_ENV})
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise ProjectError("git clone failed: %s" % e, exit_code=1) from None


def submodule_update(repo: Path) -> None:
    # The submodules' URLs are the tree's .gitmodules: not a local path either, which git
    # itself refuses a submodule since a local clone copies what the path's links name.
    env = {**os.environ, **_ENV, "GIT_ALLOW_PROTOCOL": "https:ssh"}
    try:
        subprocess.run(
            [GIT, *_QUIET, "-C", str(repo), "submodule", "update", "--init", "--recursive"],
            check=True, env=env,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise ProjectError("git submodule update failed: %s" % e, exit_code=1) from None


def pip_install(spec: str) -> None:
    try:
        # (-P: the working directory is not on the child's import path, where a `pip.py`
        # in a project someone else made would run in its place)
        subprocess.run([sys.executable, "-P", "-m", "pip", "install", spec], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise ProjectError("pip install %s failed: %s" % (spec, e), exit_code=1) from None
