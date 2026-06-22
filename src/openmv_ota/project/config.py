"""The committed config (``openmv-ota.toml``) and the gitignored local file
(``openmv-ota.local.toml``).

TOML is read with the standard library (``tomllib``, Python 3.11+). The small
amount of TOML we *write* is rendered from a template string, so no TOML writer
dependency is needed.
"""

from __future__ import annotations

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
    version: int = 1                    # OTA app release version (payload_version)
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
        name=str(product.get("name") or path.parent.name),
        vendor=product.get("vendor"),
        boards=boards,
        ota=bool(ota.get("enabled", False)),
        version=int(ota.get("version", 1)),
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


def render_config(
    name: str,
    vendor: str | None,
    boards: list[str],
    ota: bool = False,
    version: int = 1,
    signing_key_id: int | None = None,
) -> str:
    board_list = ", ".join('"%s"' % b for b in boards)
    vendor_line = ('vendor = "%s"\n' % vendor) if vendor else '# vendor = "Acme Robotics"\n'
    if ota:
        ota_section = (
            "[ota]\n"
            "enabled = true            # each partition holds a regular + golden image\n"
            "version = %d              # app release version (payload_version); bump per release\n"
            % version
            + "signing_key_id = %d       # current OTA signing key (in keys/trusted_keys.json)\n\n"
            % (signing_key_id or 0)
        )
    else:
        ota_section = (
            "# [ota]\n"
            "# enabled = true          # opt in to over-the-air updates; halves the\n"
            "#                           usable image size (regular + golden image)\n\n"
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
        + "[targets]\n"
        "boards = [%s]\n\n" % board_list
        + "# Optional per-board settings:\n"
        "# [targets.OPENMV_AE3]\n"
        "# partitions = [0, 1]       # target both cores (HP + HE); default [0]\n"
        "# board_id = 1234           # applies to all the board's partitions\n"
        "# partition_size = 25165824 # override geometry (single-partition only)\n"
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
