"""The committed config (``openmv-ota.toml``) and the gitignored local file
(``openmv-ota.local.toml``).

TOML is read with the standard library (``tomllib``, Python 3.11+). The small
amount of TOML we *write* is rendered from a template string, so no TOML writer
dependency is needed.
"""

from __future__ import annotations

import binascii
import re
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
    account_id: str = ""               # the maker's OTA account (baked into system.json; '' = self-host)
    # RECOVERY CONFIG -- baked into the FIRMWARE, not the romfs.
    #
    # These are the maker's and constant per build, and a device whose romfs is gone still needs
    # them to reach the server: that is precisely why recovery could not work while they lived in
    # the app. `ca` is a path relative to the project root; empty means the bundled Mozilla set.
    server_url: str = ""
    ca: str = ""
    # Opt DOWN to one slot. Asymmetric on purpose: A/B is derived wherever it fits, and this is
    # the only way to refuse it. Named for what you GET, because the cost is invisible when you
    # choose it -- without a B slot a failed update needs a network round trip to recover, and a
    # device that cannot reach the network needs physical reflashing.
    single_image: bool = False
    # Boots a newly-installed image gets to call confirm() before it is rejected and the device
    # falls back. 1 is v1's one-shot behaviour; the default 3 buys tolerance of a transient boot
    # failure (a sensor that fails to initialise on a long cable is already documented in this
    # project) at the cost of one reboot per extra attempt on an image that is genuinely bad.
    max_attempts: int = 3
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


def _max_attempts(ota: dict) -> int:
    """``[ota].max_attempts``, validated. Must be at least 1 -- zero would reject every image on
    its first boot, i.e. make the device permanently un-updatable, and it is the kind of typo
    (or clever-looking "disable trials" guess) worth catching at build time rather than in the
    field. Capped at the attempt region's size, which is what the flash can actually record."""
    value = ota.get("max_attempts", 3)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ProjectError("[ota].max_attempts must be an integer, got %r" % (value,)) from None
    if not 1 <= value <= 64:
        raise ProjectError(
            "[ota].max_attempts must be between 1 and 64, got %d. 1 gives a new image a single "
            "boot to confirm itself; more tolerates a transient boot failure at the cost of one "
            "reboot per extra attempt on an image that is genuinely bad." % value)
    return value


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
        account_id=str(product.get("account_id") or ""),
        server_url=str(ota.get("server_url") or ""),
        ca=str(ota.get("ca") or ""),
        single_image=bool(ota.get("single_image", False)),
        max_attempts=_max_attempts(ota),
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


def derive_product_id(product: str, board: str) -> int:
    """A stable, auto-assigned product id for a target, so the user never has to
    invent or track a number. Seeded deterministically from ``product:board`` —
    distinct per board within a project, and reproducible (two machines, or a lost
    config, regenerate the same value). It is written into the config once at
    ``new`` and is the cross-flash guard, so keep it stable once devices ship; it
    stays overridable. Never 0 (0 means "unset")."""
    bid = binascii.crc32(("%s:%s" % (product, board)).encode("utf-8")) & 0xFFFFFFFF
    return bid or 1


def _render_target(product: str, board: str) -> str:
    """An active ``[targets.<board>]`` section with an auto-assigned product_id and a
    board_name that defaults to the product (aligned comments)."""
    bid = str(derive_product_id(product, board))
    name_val = '"%s"' % product
    w = max(len(bid), len(name_val))
    return (
        "[targets.%s]\n" % board
        + "product_id   = %s%s  # stable product id (auto-assigned; keep it once devices ship)\n"
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
    ca: str | None = None,
) -> str:
    board_list = ", ".join('"%s"' % b for b in boards)
    vendor_line = ('vendor = "%s"\n' % vendor) if vendor else '# vendor = "Acme Robotics"\n'
    if ota:
        ota_section = (
            "[ota]\n"
            "enabled = true            # each partition holds two updatable images (A/B)\n"
            "signing_key_id = %d       # current OTA signing key (in keys/trusted_keys.json)\n"
            "#                           (the app version lives in app/settings.json)\n"
            "\n"
            "# Where devices fetch updates, and what they trust. BAKED INTO THE FIRMWARE, not the\n"
            "# romfs -- a device whose image is gone still needs both to reach the server, which is\n"
            "# exactly the case recovery exists for.\n"
            "# server_url = \"https://updates.example.com\"\n"
            % (signing_key_id or 0)                 # binds to the literal chain above, not below
            + (('ca = "%s"   # TLS roots for OTA downloads (relative to the project)\n' % ca)
               if ca else
               "# ca = \"certs/root.pem\"   # relative to the project; unset = the bundled public CAs\n")
            + "\n"
            "# single_image = true     # OPT OUT of A/B: run one slot and buy back a full image of\n"
            "#                           flash. The trade, which is invisible until it bites:\n"
            "#                           a failed update then recovers by re-downloading, so a\n"
            "#                           device that cannot reach the network needs a physical reflash.\n"
            "#                           Otherwise this is derived -- A/B wherever two slots fit.\n"
            "\n"
            "# max_attempts = 3        # boots a newly-installed image gets to call confirm()\n"
            "#                           before it is rejected and the device falls back. 1 gives\n"
            "#                           it a single try; higher tolerates a transient boot failure\n"
            "#                           at the cost of one reboot per extra attempt on an image\n"
            "#                           that is genuinely bad. Retries only help a failure that\n"
            "#                           RESETS -- a hang just hangs this many times.\n\n"
        )
    else:
        ota_section = (
            "# [ota]\n"
            "# enabled = true          # opt in to over-the-air updates; halves the\n"
            "#                           usable image size (regular + golden image)\n\n"
        )
    # Active per-board sections with an auto-assigned product_id in EVERY mode -- one
    # scaffold shape. In a plain project the id is inert (recorded in system.json,
    # nothing enforces it; delete the line to re-derive from the product name); under
    # OTA it is the cross-flash guard fielded devices bake in.
    targets = (
        "[targets]\nboards = [%s]\n\n" % board_list
        + "".join(_render_target(name, b) for b in boards)
        + "# A board's table can also set partition_size = N to override the firmware\n"
        "# partition geometry. Multi-core boards (e.g. AE3) build every partition\n"
        "# automatically -- the coprocessor core's romfs is built from app-coprocessor/.\n"
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


def set_signing_key_id(path: Path, new_id: int) -> None:
    """Update ``[ota].signing_key_id`` in-place in the config file, preserving the
    rest of the text (comments, formatting). Raises if the key isn't present."""
    text = path.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r"signing_key_id\s*=\s*\d+", "signing_key_id = %d" % new_id, text, count=1
    )
    if n == 0:
        raise ProjectError("could not find signing_key_id in %s" % path.name)
    path.write_text(new_text, encoding="utf-8")


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
