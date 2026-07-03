"""Admin API scopes (kept dependency-free so the CLI can reference them on a base install)."""

from __future__ import annotations

# Flat operation names -- a token's scope list reads like a to-do list of what it can do:
# publish  -> publish releases
# manage   -> all fleet changes: rollouts (create/raise/pause/rollback), cohorts, pins, device binds
# observe  -> read everything (fleet, releases, rollouts, devices, audit)
# These are the per-account operations; an account's tokens carry (a subset of) them.
SCOPES = ("publish", "manage", "observe")

# accounts is a *privileged operator* scope -- it mints/lists accounts, so it is NOT part of the
# per-account default set (an account admin must not be able to create other accounts). Only the
# root/bootstrap token (and tokens an operator explicitly issues it to) carries it.
ACCOUNT_ADMIN = "accounts"
ALL_SCOPES = (*SCOPES, ACCOUNT_ADMIN)
