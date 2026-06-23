"""The committed config (``openmv-ota.toml``) and the gitignored local file
(``openmv-ota.local.toml``).

TOML is read with the standard library (``tomllib``, Python 3.11+). The small
amount of TOML we *write* is rendered from a template string, so no TOML writer
dependency is needed.
"""

from __future__ import annotations

import binascii
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from openmv_ota.romfs import boards as boards_mod

from .errors import ProjectError

CONFIG_NAME = "openmv-ota.toml"
LOCAL_NAME = "openmv-ota.local.toml"


@dataclass
class OtaConfig:
    name: str
    vendor: str | None
    boards: list[str]
    ota: bool = False
    signing_key_id: int | None = None  # current OTA signing key
    overrides: dict[str, dict] = field(default_factory=dict)


@dataclass
class LocalConfig:
    firmware_path: Path
    sdk_home: Path | None = None


def _loads(text: str, what: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProjectError("%s is not valid TOML: %s" % (what, e)) from None


def load_config(path: Path) -> OtaConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ProjectError("no %s found (is this a project directory?)" % CONFIG_NAME) from None
    return parse_config(text, path.parent.name)


def parse_config(text: str, default_name: str) -> OtaConfig:
    """Parse config TOML text. Used by ``load_config`` and at ``new`` time, so the
    object the digest/resolve see is exactly what was rendered to disk."""
    data = _loads(text, CONFIG_NAME)

    product = data.get("product", {})
    targets = data.get("targets", {})
    boards = targets.get("boards")
    if not isinstance(boards, list) or not boards or not all(isinstance(b, str) for b in boards):
        raise ProjectError("[targets].boards must be a non-empty list of board names")
    validate_boards(boards)

    overrides = {k: v for k, v in targets.items() if k != "boards" and isinstance(v, dict)}
    ota = data.get("ota", {}) or {}
    signing_key_id = ota.get("signing_key_id")
    return OtaConfig(
        name=str(product.get("name") or default_name),
        vendor=product.get("vendor"),
        boards=boards,
        ota=bool(ota.get("enabled", False)),
        signing_key_id=int(signing_key_id) if signing_key_id is not None else None,
        overrides=overrides,
    )


def validate_boards(boards: list[str]) -> None:
    for name in boards:
        try:
            boards_mod.get_board(name)
        except KeyError as e:
            raise ProjectError(str(e)) from None


def load_local(path: Path) -> LocalConfig | None:
    """Load the gitignored local file, or ``None`` if it does not exist."""
    if not path.exists():
        return None
    data = _loads(path.read_text(encoding="utf-8"), LOCAL_NAME)
    fw = data.get("firmware", {})
    fw_path = fw.get("path")
    if not fw_path:
        raise ProjectError("%s is missing [firmware].path" % LOCAL_NAME)
    sdk_home = (data.get("sdk", {}) or {}).get("home") or None
    return LocalConfig(
        firmware_path=Path(fw_path),
        sdk_home=Path(sdk_home) if sdk_home else None,
    )


def derive_board_id(product: str, board: str) -> int:
    """A stable, auto-assigned product id for a target, so the user never has to
    invent or track a number. Seeded deterministically from ``product:board`` —
    distinct per board within a project, and reproducible (two machines, or a lost
    config, regenerate the same value). It is written into the config once at
    ``new`` and is the cross-flash guard, so keep it stable once devices ship; it
    stays overridable. Never 0 (0 means "unset")."""
    bid = binascii.crc32(("%s:%s" % (product, board)).encode("utf-8")) & 0xFFFFFFFF
    return bid or 1


def _render_target(product: str, board: str) -> str:
    """An active ``[targets.<board>]`` section with an auto-assigned board_id and a
    board_name that defaults to the product (aligned comments)."""
    bid = str(derive_board_id(product, board))
    name_val = '"%s"' % product
    w = max(len(bid), len(name_val))
    return (
        "[targets.%s]\n" % board
        + "board_id   = %s%s  # stable product id (auto-assigned; keep it once devices ship)\n"
        % (bid, " " * (w - len(bid)))
        + "board_name = %s%s  # human label; defaults to the product name, rename freely\n\n"
        % (name_val, " " * (w - len(name_val)))
    )


def render_config(
    name: str,
    vendor: str | None,
    boards: list[str],
    ota: bool = False,
    signing_key_id: int | None = None,
) -> str:
    board_list = ", ".join('"%s"' % b for b in boards)
    vendor_line = ('vendor = "%s"\n' % vendor) if vendor else '# vendor = "Acme Robotics"\n'
    if ota:
        ota_section = (
            "[ota]\n"
            "enabled = true            # each partition holds a regular + golden image\n"
            "signing_key_id = %d       # current OTA signing key (in keys/trusted_keys.json)\n"
            "#                           (the app version lives in app/settings.json)\n\n"
            % (signing_key_id or 0)
        )
    else:
        ota_section = (
            "# [ota]\n"
            "# enabled = true          # opt in to over-the-air updates; halves the\n"
            "#                           usable image size (regular + golden image)\n\n"
        )
    if ota:
        # Active per-board sections with an auto-assigned board_id (the cross-flash
        # guard); the user can rename board_name and override board_id.
        targets = (
            "[targets]\nboards = [%s]\n\n" % board_list
            + "".join(_render_target(name, b) for b in boards)
            + "# A board's table can also set partitions = [0, 1] (target multiple ROMFS\n"
            "# partitions, e.g. AE3's two cores; default [0]) or partition_size = N\n"
            "# (override the firmware partition geometry, single-partition only).\n"
        )
    else:
        targets = (
            "[targets]\nboards = [%s]\n\n" % board_list
            + "# Optional per-board settings (add one table per board to configure):\n"
            "# [targets.OPENMV_AE3]\n"
            "# partitions = [0, 1]       # target both cores (HP + HE); default [0]\n"
            "# partition_size = 25165824 # override geometry (single-partition only)\n"
            "# board_id   = 1234         # product id in /rom/system.json (auto-set in OTA mode)\n"
        )
    return (
        "# openmv-ota project config (committed, shared with your team / CI).\n"
        "# No machine paths here - the firmware checkout path lives in\n"
        "# openmv-ota.local.toml, which is gitignored.\n\n"
        "[product]\n"
        'name = "%s"\n' % name
        + vendor_line
        + "# support_period = \"5y\"\n"
        "# security_contact = \"security@example.com\"\n"
        "# disclosure_url = \"https://example.com/.well-known/security.txt\"\n\n"
        + ota_section
        + targets
    )


def render_local(firmware_path: Path, sdk_home: Path | None) -> str:
    home_line = ('home = "%s"\n' % sdk_home.as_posix()) if sdk_home else 'home = ""\n'
    return (
        "# Machine-local settings for openmv-ota (gitignored - never commit).\n\n"
        "[firmware]\n"
        'path = "%s"\n\n' % firmware_path.as_posix()
        + "[sdk]\n"
        "# Empty => ~/openmv-sdk-<SDK_VERSION>.\n"
        + home_line
    )
