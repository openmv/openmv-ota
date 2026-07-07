"""``keys/backends.json`` -- per-key signer backend records (committed, non-secret).

Maps a trusted ``key_id`` to how *this project* reaches that key's private material: the default is
a local encrypted PEM (no record needed); a record selects ``pkcs11`` / ``aws-kms`` / ``gcp-kms`` /
``azure-kms`` / ``custom`` with **non-secret** references (ARNs, token/object labels, module paths).
Secrets (PINs, cloud credentials) live in ``openmv-ota.local.toml`` or the ambient cloud
credentials, never here. Committed so teammates + CI can sign without reconfiguring.
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import ProjectError

BACKENDS_NAME = "backends.json"


def backends_path(root: str | Path) -> Path:
    return Path(root) / "keys" / BACKENDS_NAME


def read_backends(root: str | Path) -> dict[int, dict]:
    """The ``{key_id: record}`` map (empty if the file is absent). Keys are hex strings on disk."""
    p = backends_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ProjectError("keys/backends.json is not valid JSON: %s" % e) from None
    return {int(k, 0): v for k, v in data.items()}


def write_backends(root: str | Path, records: dict[int, dict]) -> None:
    p = backends_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {("0x%04x" % kid): rec for kid, rec in sorted(records.items())}
    p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
