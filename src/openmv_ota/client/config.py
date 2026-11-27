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


# The OpenMV-hosted update service -- what the server URL resolves to when no flag, env var,
# or saved profile names one. A fresh `pip install openmv-ota` has none of those set, and the
# hosted service is the default deployment, so the out-of-the-box experience is `client login
# --token <tok>` and go; a self-host overrides it through any of the three. The scaffolded
# main.py spells the same URL out as a literal so the device side is visible and editable.
DEFAULT_SERVER_URL = "https://ota.cloud.openmv.io"


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
    # A new file, never world-readable, renamed over the old: opened in place, an existing file
    # held the token under its own mode until the chmod, and a link there took it elsewhere.
    tmp = p.with_name(".%s.%d" % (p.name, os.getpid()))
    tmp.unlink(missing_ok=True)                    # a crashed save's, or a link: not followed
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write('[server]\nurl = "%s"\n\n[auth]\ntoken = "%s"\n' % (server_url, token))
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return p


def remove(path: Path | None = None) -> bool:
    p = path or config_path()
    if p.exists():
        p.unlink()
        return True
    return False


def resolve(flag_server: str | None, flag_token: str | None,
            path: Path | None = None) -> ClientConfig:
    """The effective server URL + token: flag > env > saved file > (URL only) the OpenMV-hosted
    service. Raises ``ClientError`` if the token can't be resolved -- there is no default
    credential, only a default place to present one."""
    cfg = load(path)
    server = (flag_server or os.environ.get("OPENMV_OTA_SERVER")
              or (cfg.server_url if cfg else "") or DEFAULT_SERVER_URL)
    token = flag_token or os.environ.get("OPENMV_OTA_TOKEN") or (cfg.token if cfg else "")
    if not token:
        raise ClientError("no API token -- pass --token, set OPENMV_OTA_TOKEN, or `client login`")
    return ClientConfig(server_url=server.rstrip("/"), token=token)
