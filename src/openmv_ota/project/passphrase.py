"""Resolve the passphrase that decrypts a project's signing keys, and where it came from.

Signing keys are always encrypted at rest. A build/project op resolves the passphrase in priority
order: a project's gitignored ``keys/.dev-passphrase`` (a throwaway **dev** key -> source
``"dev"``, which the production build rail refuses), an explicit ``--key-passphrase-file``, the
``OPENMV_OTA_KEY_PASSPHRASE`` env var, or an interactive prompt. The ``"dev"`` source is
**structural** -- the passphrase literally came from the dev file -- not a flippable config flag.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .errors import ProjectError

DEV_PASSPHRASE_NAME = ".dev-passphrase"
ENV_VAR = "OPENMV_OTA_KEY_PASSPHRASE"


def dev_passphrase_path(root: str | Path) -> Path:
    return Path(root) / "keys" / DEV_PASSPHRASE_NAME


def resolve_passphrase(root: str | Path, *, passphrase_file: str | Path | None = None,
                       interactive: bool = True) -> tuple[str, str]:
    """Return ``(passphrase, source)`` where source is ``"dev"`` or ``"user"``. Raises
    ``ProjectError`` if the key is encrypted but no passphrase can be found."""
    dev = dev_passphrase_path(root)
    if dev.exists():
        return dev.read_text(encoding="utf-8").strip(), "dev"
    if passphrase_file:
        return Path(passphrase_file).read_text(encoding="utf-8").strip(), "user"
    env = os.environ.get(ENV_VAR)
    if env:
        return env, "user"
    if interactive and sys.stdin.isatty():  # pragma: no cover  (interactive prompt)
        import getpass
        return getpass.getpass("signing key passphrase: "), "user"
    raise ProjectError(
        "the signing key is encrypted -- pass --key-passphrase-file, set %s, or use a --dev "
        "project" % ENV_VAR)
