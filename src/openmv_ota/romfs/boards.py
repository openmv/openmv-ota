"""Per-board ROMFS configuration.

Loads the bundled ``data/boards.json`` (extracted from the OpenMV IDE's
``settings.json``) and exposes each board's ROMFS partitions and per-extension
alignment rules. The core builder only needs the alignment rules and partition
sizes; ``mpy_args`` and ``npu`` are carried through for the future
model-compile layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any


@dataclass(frozen=True)
class Partition:
    name: str
    index: int
    size: int
    alignment_rules: list[dict[str, Any]] = field(default_factory=list)
    npu: dict[str, Any] | None = None


@dataclass(frozen=True)
class BoardConfig:
    name: str
    display_name: str
    arch: str
    mpy_args: list[str]
    partitions: list[Partition]

    def partition(self, index: int | None = None) -> Partition:
        """Return the partition with the given ``index`` (default: the first).

        Raises ``LookupError`` if the index is not present.
        """
        if index is None:
            return self.partitions[0]
        for p in self.partitions:
            if p.index == index:
                return p
        raise LookupError(
            "board %r has no partition index %d (available: %s)"
            % (self.name, index, ", ".join(str(p.index) for p in self.partitions))
        )


def _load_raw() -> dict[str, Any]:
    text = files("openmv_ota").joinpath("data/boards.json").read_text(encoding="utf-8")
    return json.loads(text)


def load_boards() -> dict[str, BoardConfig]:
    """Return every known board keyed by its firmware-folder name."""
    raw = _load_raw()
    boards: dict[str, BoardConfig] = {}
    for name, b in raw["boards"].items():
        parts = [
            Partition(
                name=p.get("name", ""),
                index=int(p.get("index", 0)),
                size=int(p.get("size", 0)),
                alignment_rules=list(p.get("alignment_rules", [])),
                npu=p.get("npu"),
            )
            for p in b.get("partitions", [])
        ]
        boards[name] = BoardConfig(
            name=name,
            display_name=b.get("display_name", name),
            arch=b.get("arch", ""),
            mpy_args=list(b.get("mpy_args", [])),
            partitions=parts,
        )
    return boards


def get_board(name: str) -> BoardConfig:
    """Look up one board, with a helpful error listing valid names."""
    boards = load_boards()
    try:
        return boards[name]
    except KeyError:
        raise KeyError(
            "unknown board %r. Known boards: %s"
            % (name, ", ".join(sorted(boards)))
        ) from None


def board_names() -> list[str]:
    return sorted(load_boards())
