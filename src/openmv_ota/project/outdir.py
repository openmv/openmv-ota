"""A project's files are its own: where its build output goes, and what a link in it is."""

from __future__ import annotations

import os
from pathlib import Path

from .errors import ProjectError

_MSG = "%s is a link: a project's files are its own -- remove it"


def _links(top: Path, files_only: bool = False):
    """Every link under ``top`` (``top`` itself first), not followed. With ``files_only``
    a link to a directory passes: where a directory is kept is the user's business."""
    if top.is_symlink() and not (files_only and top.is_dir()):
        yield top
    for base, dirs, files in os.walk(top):
        for name in sorted(dirs + files):
            path = Path(base) / name
            if path.is_symlink() and not (files_only and path.is_dir()):
                yield path


def check_project(root: str | Path, error: type = ProjectError) -> None:
    """Refuse a project with a link where the tools write, or read to pack and publish.

    The project may be one someone else made. A file of it that is a link (the lock, the
    ledger, a key file, anything in ``build/``) sends a write to whatever it names, with
    content the project chose, or packs what it names into an image that is then signed and
    published. ``app/`` is checked where it is staged."""
    root = Path(root)
    found = []
    if root.is_dir():
        with os.scandir(root) as entries:
            found = [Path(e.path) for e in entries if e.is_symlink() and not e.is_dir()]
    for name, files_only in ((".openmv-ota", False), ("build", False), ("keys", True)):
        found += _links(root / name, files_only)
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
