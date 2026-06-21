"""Resolve all submodule commits for the snapshot."""

from __future__ import annotations

from pathlib import Path

from .. import gitrepo


def resolve_submodules(repo: Path) -> list[dict]:
    """Return ``[{path, commit, describe, initialized}, ...]`` sorted by path."""
    entries = gitrepo.submodule_status(repo)
    return sorted(entries, key=lambda e: e["path"])
