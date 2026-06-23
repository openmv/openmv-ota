"""Firmware-pegged OTA projects.

`openmv-ota project` creates a project directory that pegs an OTA project to a
local OpenMV firmware checkout and records a reproducible snapshot (the *lock*)
of every version and per-board geometry the downstream layers need.

The public contract for those downstream layers is :func:`load_project`, which
returns a :class:`~openmv_ota.project.project.LoadedProject` exposing the parsed
lock plus this machine's resolved firmware path, SDK home, and toolchain binary
paths.
"""

from __future__ import annotations

from .project import LoadedProject, ProjectPaths, load_project

__all__ = ["LoadedProject", "ProjectPaths", "load_project"]
