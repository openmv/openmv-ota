"""Which firmware binaries this machine built: the record ``build firmware`` keeps outside the
project, and the check every command that signs or flashes one makes against it.

A boot-stub board's image carries its firmware, signed with the project's keys, and ``flash
factory`` writes the same files to a camera. They were read from the project's ``build/`` with
nothing to say where they came from, so a project handed over as a directory or an archive (or
with ``build/`` committed) had a binary planted there signed into the release. So each binary
``build firmware`` (and ``build stub``) writes is recorded here -- its resolved path, sha256 and
size, the firmware lock's commits, the project it was built for and the digest of what that
build fed the firmware (``firmware._inputs_digest``: the frozen modules, the generated
``_ota_config`` with the payload keys, the overlay) -- and a binary that does not match its
record, was built for another project (one ``-o`` directory shared by two), or names other
commits than the lock is refused, naming the file. The binaries one image or one flash takes
together must come from one build (see :func:`one_build`): a run that failed part way leaves
what it wrote unrecorded, so it never pairs with what an earlier run left.

The record lives in the machine's state directory (``$XDG_STATE_HOME/openmv-ota/firmware``, or
``~/.local/state``; ``%LOCALAPPDATA%\\openmv-ota\\state`` on Windows), never in the project: whoever
can write the project could write a record beside the binary. One small file per binary, named
by its path, so two builds at once never race on one file.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .errors import BuildError


def state_dir(os_name: str | None = None) -> Path:
    """This machine's state directory for the tool. A relative ``XDG_STATE_HOME`` is ignored, as
    the XDG spec says: it would be read against the working directory, which may be the project.
    ``os_name`` defaults to ``os.name`` and is injectable for tests."""
    if (os_name if os_name is not None else os.name) == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "openmv-ota" / "state"
    xdg = os.environ.get("XDG_STATE_HOME", "")
    base = Path(xdg) if os.path.isabs(xdg) else Path.home() / ".local" / "state"
    return base / "openmv-ota"


def _record_path(resolved: str) -> Path:
    return state_dir() / "firmware" / (hashlib.sha256(resolved.encode()).hexdigest() + ".json")


def _commits(lock) -> dict:
    return {"firmware_commit": lock.firmware.get("commit"),
            "micropython_commit": lock.micropython.get("commit")}


def record(path: str | Path, lock, root: str | Path, inputs: str | None) -> None:
    """Note that this machine's ``build firmware`` (or ``build stub``) wrote ``path`` for the
    project at ``root``, from the firmware ``lock`` pins and the build ``inputs`` (None for a
    stub built alone)."""
    from openmv_ota.client.config import write_private

    resolved = os.path.realpath(path)
    data = Path(resolved).read_bytes()
    entry = {"path": resolved, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
             **_commits(lock), "project": os.path.realpath(root), "inputs": inputs}
    dest = _record_path(resolved)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(dest, (json.dumps(entry, indent=2) + "\n").encode())


def check(path: str | Path, data: bytes, lock, root: str | Path,
          error: type = BuildError) -> dict:
    """The record of ``data``, read from ``path``, when it is what this machine's ``build
    firmware`` wrote there for the project at ``root`` from the commits ``lock`` pins; otherwise
    ``error`` naming the file."""
    resolved = os.path.realpath(path)
    digest = hashlib.sha256(data).hexdigest()
    try:
        entry = json.loads(_record_path(resolved).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        entry = None
    if not isinstance(entry, dict) or entry.get("path") != resolved:
        raise error("%s (sha256 %s) was not built by `openmv-ota build firmware` on this machine: "
                    "a firmware binary is signed or flashed only as this machine built it -- run "
                    "`openmv-ota build firmware`" % (path, digest), exit_code=1)
    if entry.get("sha256") != digest or entry.get("size") != len(data):
        raise error("%s (sha256 %s) changed since `openmv-ota build firmware` wrote it (sha256 %s)"
                    " -- run `openmv-ota build firmware`" % (path, digest, entry.get("sha256")),
                    exit_code=1)
    want = _commits(lock)
    if {k: entry.get(k) for k in want} != want:
        raise error("%s was built from firmware %s, not the %s the lock pins -- run `openmv-ota "
                    "build firmware`" % (path, entry.get("firmware_commit"),
                                         want["firmware_commit"]), exit_code=1)
    if entry.get("project") != os.path.realpath(root):
        raise error("%s was built for the project at %s -- run `openmv-ota build firmware` for "
                    "this one" % (path, entry.get("project")), exit_code=1)
    return entry


def one_build(entries: list[dict], error: type = BuildError) -> None:
    """Refuse firmware binaries, taken together, that more than one build wrote: new firmware
    beside the second region or second core an earlier build left is no firmware at all."""
    if len({e.get("inputs") for e in entries}) > 1:
        raise error("%s were written by different `openmv-ota build firmware` runs -- run it "
                    "again" % ", ".join(e["path"] for e in entries), exit_code=1)
