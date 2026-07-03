"""Admin API scopes (kept dependency-free so the CLI can reference them on a base install)."""

from __future__ import annotations

# release:write  -> publish releases
# rollout:control -> create/raise/pause/rollback rollouts
# fleet:read     -> read fleet status + audit
# These are the per-account operations; an account's tokens carry (a subset of) them.
SCOPES = ("release:write", "rollout:control", "fleet:read")

# account:admin is a *privileged operator* scope -- it mints/lists accounts, so it is NOT part of
# the per-account default set (an account admin must not be able to create other accounts). Only
# the root/bootstrap token (and tokens an operator explicitly issues it to) carries it.
ACCOUNT_ADMIN = "account:admin"
ALL_SCOPES = (*SCOPES, ACCOUNT_ADMIN)
