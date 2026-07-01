"""Admin API scopes (kept dependency-free so the CLI can reference them on a base install)."""

from __future__ import annotations

# release:write  -> publish releases
# rollout:control -> create/raise/pause/rollback rollouts
# fleet:read     -> read fleet status + audit
SCOPES = ("release:write", "rollout:control", "fleet:read")
