"""Build an OpenMV ROMFS image from a directory tree, and read one back.

This is the core, dependency-free builder. It packs files **verbatim** (no
mpy-cross / NPU model conversion — those are a later layer) and applies the
board's per-extension alignment rules so memory-mapped assets land on the right
boundary.

Directory traversal is sorted by name for reproducible, deterministic output:
the same input tree always produces byte-identical bytes. The image contains the
*contents* of ``src_dir`` at the ROMFS root (the top directory itself is not
wrapped), matching the IDE.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from typing import Any

from .boards import BoardConfig, Partition
from .container import ROMFS_MIN_ALIGNMENT, VfsRomReader, VfsRomWriter

# Patterns excluded by default (matched against each entry's base name). Covers
# the usual build/VCS/editor cruft that should never reach a device image.
DEFAULT_EXCLUDES = (
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".git",
    ".svn",
    ".hg",
    ".DS_Store",
    "Thumbs.db",
    "*.swp",
)


class BuildError(Exception):
    """Raised when an image cannot be built (e.g. it exceeds the partition)."""


def _excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def build_image(
    src_dir: str,
    alignment_rules: list[dict[str, Any]] | None = None,
    default_alignment: int = ROMFS_MIN_ALIGNMENT,
    exclude: list[str] | None = None,
    follow_symlinks: bool = False,
) -> bytes:
    """Pack ``src_dir`` into a ROMFS image using ``alignment_rules``.

    Files are added verbatim. Entries are visited in sorted (case-sensitive)
    name order for determinism. ``exclude`` is a list of ``fnmatch`` patterns
    matched against each entry's base name (a matched directory is skipped
    whole). ``default_alignment`` is the fallback for extensions with no rule.
    Symlinks are skipped unless ``follow_symlinks`` is set.
    """
    if not os.path.isdir(src_dir):
        raise BuildError("not a directory: %s" % src_dir)

    patterns = list(exclude or [])
    writer = VfsRomWriter(alignment_rules or [], default_alignment=default_alignment)

    def walk(path: str) -> None:
        for entry in sorted(os.scandir(path), key=lambda e: e.name):
            if _excluded(entry.name, patterns):
                continue
            if entry.is_symlink() and not follow_symlinks:
                continue
            if entry.is_dir():
                writer.opendir(entry.name)
                walk(entry.path)
                writer.closedir()
            elif entry.is_file():
                with open(entry.path, "rb") as f:
                    writer.mkfile(entry.name, f.read())

    walk(src_dir)
    return writer.finalize()


@dataclass
class BuildResult:
    image: bytes
    partition: Partition | None
    alignment_rules: list[dict[str, Any]]

    @property
    def size(self) -> int:
        return len(self.image)

    @property
    def capacity(self) -> int | None:
        return self.partition.size if self.partition else None

    @property
    def free(self) -> int | None:
        return (self.capacity - self.size) if self.capacity is not None else None


def resolve_rules(
    partition: Partition,
    extra_rules: list[dict[str, Any]] | None = None,
    use_board_rules: bool = True,
) -> list[dict[str, Any]]:
    """The effective alignment rules: board partition defaults (unless disabled)
    with ``extra_rules`` overriding by extension. Shared by build and verify."""
    rules: list[dict[str, Any]] = []
    if use_board_rules:
        rules.extend(partition.alignment_rules)
    if extra_rules:
        rules = merge_rules(rules, extra_rules)
    return rules


def build_for_board(
    src_dir: str,
    board: BoardConfig,
    partition_index: int | None = None,
    extra_rules: list[dict[str, Any]] | None = None,
    use_board_rules: bool = True,
    default_alignment: int = ROMFS_MIN_ALIGNMENT,
    exclude: list[str] | None = None,
    follow_symlinks: bool = False,
    max_size: int | None = None,
    allow_oversize: bool = False,
) -> BuildResult:
    """Build for a specific board partition, enforcing the partition capacity.

    ``extra_rules`` override the board's alignment rules by extension.
    ``max_size`` overrides the partition size for the capacity check.
    """
    partition = board.partition(partition_index)
    rules = resolve_rules(partition, extra_rules, use_board_rules)

    image = build_image(
        src_dir, rules, default_alignment=default_alignment,
        exclude=exclude, follow_symlinks=follow_symlinks,
    )

    cap = max_size if max_size is not None else partition.size
    if cap and len(image) > cap and not allow_oversize:
        raise BuildError(
            "image is %d bytes but partition %r (%s) holds only %d bytes "
            "(%d over). Use --allow-oversize to override."
            % (len(image), partition.name, board.name, cap, len(image) - cap)
        )

    return BuildResult(image=image, partition=partition, alignment_rules=rules)


def merge_rules(
    base: list[dict[str, Any]], extra: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Combine rule lists so ``extra`` overrides ``base`` by extension."""
    merged: dict[str, dict[str, Any]] = {}
    for rule in [*base, *extra]:
        merged[str(rule["extension"]).lower()] = {
            "extension": str(rule["extension"]).lower(),
            "alignment": int(rule["alignment"]),
        }
    return list(merged.values())


def read_image(data: bytes) -> VfsRomReader:
    """Parse a ROMFS image into a reader (tree, walk, extract)."""
    return VfsRomReader(data)


@dataclass
class VerifyResult:
    files: int
    dirs: int
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def verify_image(
    data: bytes,
    alignment_rules: list[dict[str, Any]] | None = None,
    default_alignment: int = ROMFS_MIN_ALIGNMENT,
) -> VerifyResult:
    """Parse an image and check every file payload sits on its required
    boundary. Raises ``RomfsError`` (via :func:`read_image`) if it does not
    parse at all; otherwise returns the per-file findings."""
    from .container import alignment_for

    reader = read_image(data)
    rules = alignment_rules or []
    files = dirs = 0
    problems: list[str] = []
    for path, entry in reader.walk():
        if entry.is_dir:
            dirs += 1
            continue
        files += 1
        want = alignment_for(entry.name, rules, default_alignment)
        off = entry.data_offset
        if off is not None and off % want != 0:
            problems.append(
                "%s: payload at offset %d is not %d-byte aligned" % (path, off, want)
            )
    return VerifyResult(files=files, dirs=dirs, problems=problems)
