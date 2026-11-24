"""A project's files are its own: where its build output goes, and what a link in it is."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .errors import ProjectError

# The project's directories that are read into an image: one of them as a link to another
# directory packs that directory, whatever it holds.
_PACKED = ("app", "app-coprocessor", "device", "certs")

_MSG = "%s is a link: a project's files are its own -- remove it"


def _shared(path: Path) -> bool:
    """A link, or a file with another name somewhere (a hard link): written in place, as the
    lock, the ledger and the build outputs are, it is that other file that changes."""
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or (stat.S_ISREG(st.st_mode) and st.st_nlink > 1)


def _links(top: Path):
    """Every link under ``top`` (``top`` itself first), not followed."""
    if top.is_symlink():
        yield top
    for base, dirs, files in os.walk(top):
        for name in sorted(dirs + files):
            if _shared(Path(base) / name):
                yield Path(base) / name


def check_project(root: str | Path, error: type = ProjectError) -> None:
    """Refuse a project with a link where the tools write, or read to pack and publish.

    The project may be one someone else made. A file of it that is a link (the lock, the
    ledger, a key file, anything in ``build/``) sends a write to whatever it names, with
    content the project chose, or packs what it names into an image that is then signed and
    published. The directories that are packed whole, and the ones `project new` writes
    its scaffolding into, hold no link and are none. ``keys/`` is the project's own too, the
    directory and all: as a link it is another project's keys, signed with and rewritten (keys kept
    somewhere else are a signer backend's business, in the machine's own config)."""
    root = Path(root)
    found = []
    if root.is_dir():
        with os.scandir(root) as entries:
            # (an editor's lock beside a file it has open is a link that names no file)
            found = [Path(e.path) for e in entries if _shared(Path(e.path))
                     and not e.name.startswith(".#") and not e.is_dir()]
    for name in (".openmv-ota", "build", "keys", "compliance") + _PACKED:
        found += _links(root / name)
    if found:
        raise error(_MSG % sorted(found)[0])


def output_dir(project: str | Path, output: str | Path | None, error: type = ProjectError) -> Path:
    """``output``, or the project's own ``build/``, with the project checked (see
    :func:`check_project`). An output directory named inside the project is the project's
    too, and is checked the same way."""
    check_project(project, error)
    if not output:
        return Path(project) / "build"
    if Path(os.path.realpath(project)) in Path(os.path.realpath(output)).parents:
        for path in _links(Path(output)):
            raise error(_MSG % path)
    return Path(output)
