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

from openmv_ota.ota import geometry
from openmv_ota.romfs import boards as boards_mod

from ..errors import ProjectError
from .macros import parse_defines


@dataclass(frozen=True)
class ResolvedBoard:
    name: str
    board_type: str | None
    arch: str
    mpy_args: list[str]
    npu: str | None                       # NPU type ("vela" | "stedgeai" | None)
    partition_index: int
    partition_size: int
    erase_size: int                       # flash erase block of the partition's backing store
    front_size: int                       # FRONT slot size (half the partition, block-aligned)
    alignment_rules: list[dict] = field(default_factory=list)
    geometry_source: str = "bundled"
    npu_config: dict | None = None        # full compiler config (args + file refs)
    mbedtls: bool = True                   # firmware builds mbedtls (OTA verify needs it)


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


def _mbedtls_supported(repo: Path, board: str) -> bool:
    """Whether the firmware builds mbedtls for this board, read from
    ``board_config.mk`` (``MICROPY_SSL_MBEDTLS``). The OTA boot.py verifies image
    signatures with an mbedtls-backed C module, so a board without it can't run OTA
    (the emulator boards MPS2/MPS3 set it to 0). Only an explicit ``0`` disables it;
    a missing line means the board enables mbedtls elsewhere (e.g. the Alif port)."""
    mk = repo / "boards" / board / "board_config.mk"
    if not mk.exists():
        return True
    m = re.search(r"^\s*MICROPY_SSL_MBEDTLS\s*=\s*(\d+)",
                  mk.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
    return not (m and m.group(1) == "0")


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

    npu_config = part.npu if isinstance(part.npu, dict) else None
    npu_type = npu_config.get("type") if npu_config else None
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

    resolved = ResolvedBoard(
        name=name,
        board_type=_board_type(repo, name),
        arch=cfg.arch,
        mpy_args=list(cfg.mpy_args),
        npu=npu_type,
        partition_index=part.index,
        partition_size=size,
        erase_size=part.erase_size,
        front_size=geometry.front_size(size, part.erase_size),
        alignment_rules=list(part.alignment_rules),
        geometry_source=source,
        npu_config=npu_config,
        mbedtls=_mbedtls_supported(repo, name),
    )
    return resolved, warnings
