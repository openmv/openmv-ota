"""Per-board golden + release ledger (``.openmv-ota/ledger.json``).

This is the authoritative local *state* the build reads back (the history is the raw,
append-only trail; this is the derived view it acts on):

- the **golden** each board was manufactured with -- so ``build ota-romfs`` can resolve the
  delta base automatically instead of the operator hand-pointing ``--delta-from`` at the
  right file, and so a wrong/stale base is caught;
- the **releases** shipped per board -- so a non-increasing version is refused before it's
  published.

Committable, no secrets (versions + public sha256s + a relative path only). Regenerable: a
missing/corrupt ledger reads as empty rather than failing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

LEDGER_DIR = ".openmv-ota"
LEDGER_NAME = "ledger.json"
SCHEMA = 1


def ledger_path(root: str | Path) -> Path:
    return Path(root) / LEDGER_DIR / LEDGER_NAME


def _load(root: str | Path) -> dict:
    path = ledger_path(root)
    if not path.exists():
        return {"schema": SCHEMA, "boards": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": SCHEMA, "boards": {}}
    data.setdefault("boards", {})
    return data


def _save(root: str | Path, data: dict) -> None:
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _board(data: dict, board: str) -> dict:
    return data["boards"].setdefault(board, {"golden": None, "releases": []})


def record_golden(root, board: str, *, version: str, payload_version: int,
                  sha256: str, path: str) -> None:
    """Record (overwrite) the factory golden a board was manufactured with."""
    data = _load(root)
    _board(data, board)["golden"] = {"version": version, "payload_version": payload_version,
                                     "sha256": sha256, "path": path}
    _save(root, data)


def golden_for(root, board: str) -> dict | None:
    """The recorded golden for a board (``{version, payload_version, sha256, path}``) or None."""
    return _load(root)["boards"].get(board, {}).get("golden")


def record_release(root, board: str, *, version: str, payload_version: int,
                   sha256: str, key_id: int, when: str | None = None) -> None:
    """Append a shipped OTA release for a board (``when`` defaults to now, UTC)."""
    when = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = _load(root)
    _board(data, board)["releases"].append(
        {"version": version, "payload_version": payload_version, "sha256": sha256,
         "key_id": key_id, "ts": when})
    _save(root, data)


def last_release(root, board: str) -> dict | None:
    """The most recently recorded release for a board, or None."""
    rels = _load(root)["boards"].get(board, {}).get("releases", [])
    return rels[-1] if rels else None
