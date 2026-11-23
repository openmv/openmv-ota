"""Where a project's build output goes, and comes from."""

from __future__ import annotations

import os
from pathlib import Path

from .errors import ProjectError


def output_dir(project: str | Path, output: str | Path | None, error: type = ProjectError) -> Path:
    """``output``, or the project's own ``build/``. The project may be one someone else
    made, and its ``build/`` is written to, read back into signed images, flashed and
    published: a link in it (the directory itself, or a name in it) would send a write to
    whatever it names, or pack what it names. Such a project is refused, with ``error``."""
    own = Path(project) / "build"
    linked = [own] if own.is_symlink() else []
    if not linked and own.is_dir():
        with os.scandir(own) as entries:
            linked = sorted(Path(e.path) for e in entries if e.is_symlink())
    if linked:
        raise error("%s is a link: the build output is the project's own files -- remove it"
                    % linked[0])
    return Path(output) if output else own
