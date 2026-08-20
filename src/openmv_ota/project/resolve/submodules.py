"""Resolve all submodule commits for the snapshot."""

from __future__ import annotations

from pathlib import Path

from .. import gitrepo


def resolve_submodules(repo: Path) -> list[dict]:
    """Return ``[{path, commit, describe, initialized, remote}, ...]`` sorted by path.
    ``remote`` (the .gitmodules URL, "" when unmapped) is each submodule's upstream
    identity -- it is what lets the SBOM give lwip/mbedtls a real purl."""
    remotes = gitrepo.submodule_remotes(repo)
    entries = gitrepo.submodule_status(repo)
    for e in entries:
        e["remote"] = remotes.get(e["path"], "")
    return sorted(entries, key=lambda e: e["path"])
