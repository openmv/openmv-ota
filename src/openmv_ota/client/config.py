"""The saved client profile (server URL + admin token) at ``~/.config/openmv-ota/client.toml``.

Read with the stdlib ``tomllib`` and written from a template (mirroring ``project/config.py`` --
no TOML-writer dependency). Per-invocation resolution is **flag > env > file**, so CI runs
stateless (``OPENMV_OTA_SERVER``/``OPENMV_OTA_TOKEN``) and humans ``client login`` once.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import ClientError


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "openmv-ota" / "client.toml"


@dataclass(frozen=True)
class ClientConfig:
    server_url: str
    token: str


def load(path: Path | None = None) -> ClientConfig | None:
    """The saved profile, or ``None`` if absent/unreadable."""
    p = path or config_path()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return ClientConfig(server_url=(data.get("server") or {}).get("url", ""),
                        token=(data.get("auth") or {}).get("token", ""))


def save(server_url: str, token: str, path: Path | None = None) -> Path:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[server]\nurl = "%s"\n\n[auth]\ntoken = "%s"\n' % (server_url, token),
                 encoding="utf-8")
    p.chmod(0o600)
    return p


def remove(path: Path | None = None) -> bool:
    p = path or config_path()
    if p.exists():
        p.unlink()
        return True
    return False


def resolve(flag_server: str | None, flag_token: str | None,
            path: Path | None = None) -> ClientConfig:
    """The effective server URL + token: flag > env > saved file. Raises ``ClientError`` if either
    can't be resolved."""
    cfg = load(path)
    server = flag_server or os.environ.get("OPENMV_OTA_SERVER") or (cfg.server_url if cfg else "")
    token = flag_token or os.environ.get("OPENMV_OTA_TOKEN") or (cfg.token if cfg else "")
    if not server:
        raise ClientError("no server URL -- pass --server, set OPENMV_OTA_SERVER, or `client login`")
    if not token:
        raise ClientError("no API token -- pass --token, set OPENMV_OTA_TOKEN, or `client login`")
    return ClientConfig(server_url=server.rstrip("/"), token=token)
