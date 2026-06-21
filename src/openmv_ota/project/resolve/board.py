"""Resolve per-board geometry, firmware-consistent.

Partition *geometry* (size, derived FRONT size) comes from the pegged firmware's
``boards/<BOARD>/board_config.h``; alignment rules, arch, mpy args, and NPU type
come from the bundled board defaults (``romfs/boards.py``). The two are merged
and frozen into the lock so all downstream layers read one consistent source.

Geometry precedence (recorded in ``geometry_source``):
``override`` (TOML) > ``firmware`` (a single unambiguous macro) > ``bundled``
(the default, used when the firmware value is conditional/ambiguous or absent).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from openmv_ota.romfs import boards as boards_mod

from ..errors import ProjectError
from .macros import parse_defines


@dataclass(frozen=True)
class ResolvedBoard:
    name: str
    board_type: str | None
    arch: str
    mpy_args: list[str]
    npu: str | None
    partition_index: int
    partition_size: int
    front_size: int
    alignment_rules: list[dict] = field(default_factory=list)
    board_id: int | None = None
    geometry_source: str = "bundled"


def _front_size(partition_size: int) -> int:
    return (partition_size // 2) & ~0xFFF


def _firmware_part_lengths(repo: Path, board: str, index: int) -> list[int]:
    """Distinct ``OMV_ROMFS_PART<index>_LENGTH`` values in board_config.h."""
    header = repo / "boards" / board / "board_config.h"
    if not header.exists():
        return []
    text = header.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(r"OMV_ROMFS_PART%d_LENGTH\s+(\S+)" % index)
    values: list[int] = []
    for token in pattern.findall(text):
        try:
            v = int(token, 0)
        except ValueError:
            continue
        if v not in values:
            values.append(v)
    return values


def _board_type(repo: Path, board: str) -> str | None:
    header = repo / "boards" / board / "board_config.h"
    if not header.exists():
        return None
    defines = parse_defines(header.read_text(encoding="utf-8", errors="replace"), ["OMV_BOARD_TYPE"])
    return defines.get("OMV_BOARD_TYPE")


def resolve_board(
    repo: Path,
    name: str,
    partition_index: int = 0,
    override: dict | None = None,
) -> tuple[ResolvedBoard, list[str]]:
    """Resolve one target board. Returns ``(ResolvedBoard, warnings)``."""
    override = override or {}
    warnings: list[str] = []

    try:
        cfg = boards_mod.get_board(name)
    except KeyError as e:
        raise ProjectError(str(e)) from None
    try:
        part = cfg.partition(partition_index)
    except LookupError as e:
        raise ProjectError(str(e)) from None

    npu_type = part.npu.get("type") if isinstance(part.npu, dict) else None
    fw_lengths = _firmware_part_lengths(repo, name, part.index)

    if "partition_size" in override:
        size = int(override["partition_size"])
        source = "override"
    elif len(fw_lengths) == 1:
        size = fw_lengths[0]
        source = "firmware"
        if part.size and size != part.size:
            warnings.append(
                "%s: firmware partition size %d differs from bundled default %d "
                "(using firmware)" % (name, size, part.size)
            )
    elif part.size:
        size = part.size
        source = "bundled"
        if len(fw_lengths) > 1:
            warnings.append(
                "%s: firmware partition size is build-variant conditional "
                "(%s); using bundled default %d. Set [targets.%s] partition_size "
                "to override." % (name, ", ".join(hex(v) for v in fw_lengths), size, name)
            )
    else:
        raise ProjectError(
            "%s: no partition size from firmware or bundled defaults; set "
            "[targets.%s] partition_size" % (name, name)
        )

    board_id = int(override["board_id"]) if "board_id" in override else None

    resolved = ResolvedBoard(
        name=name,
        board_type=_board_type(repo, name),
        arch=cfg.arch,
        mpy_args=list(cfg.mpy_args),
        npu=npu_type,
        partition_index=part.index,
        partition_size=size,
        front_size=_front_size(size),
        alignment_rules=list(part.alignment_rules),
        board_id=board_id,
        geometry_source=source,
    )
    return resolved, warnings
