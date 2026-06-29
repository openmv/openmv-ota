"""Append-only operations history for a project (``.openmv-ota/history.jsonl``).

Every state-changing tool run records a structured event here -- what was done, when, which
boards/artifacts/keys were involved, and the outcome -- so there is a durable, committable
record of the (often irreversible) actions taken on a project: key generation/rotation,
signing, building, publishing. It exists so an operator can reconstruct and recover from
mistakes ("which key signed v3?", "did I already ship this?") rather than destroying a fleet
silently.

**No secrets are ever written** -- key *ids* and public artifact names/hashes only -- so the
file is safe to commit (and committing it is the point: the history survives a lost laptop).
It is an audit trail, not authoritative state; the golden/release ledgers are the state the
tools read back. Recording is best-effort: a history write must never fail a build.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

HISTORY_DIR = ".openmv-ota"
HISTORY_NAME = "history.jsonl"


def history_path(root: str | Path) -> Path:
    return Path(root) / HISTORY_DIR / HISTORY_NAME


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(root: str | Path, action: str, when: str | None = None, **fields) -> None:
    """Append one event (``{"ts", "action", **fields}``) as a JSON line. Best-effort: a
    write failure (read-only tree, etc.) is swallowed so it can't fail the operation."""
    entry = {"ts": when or _utcnow_iso(), "action": action}
    entry.update(fields)
    try:
        path = history_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def read(root: str | Path) -> list[dict]:
    """Every recorded event, oldest first (``[]`` if none). A corrupt/partial line is
    skipped rather than failing the whole read."""
    path = history_path(root)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
