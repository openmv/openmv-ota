"""Admin API scopes (kept dependency-free so the CLI can reference them on a base install)."""

from __future__ import annotations

# Operation names, and a LADDER -- each rung includes the ones below it:
# observe  -> read everything (fleet, releases, rollouts, devices, audit)
# manage   -> observe, plus all fleet changes: rollouts (create/raise/pause/stop), cohorts,
#             pins, device binds
# publish  -> manage, plus publishing releases
# A token names its highest rung (the API still takes a list); ``expand`` fills in the
# implied rungs, so a "publish" token can also list the releases it just published.
SCOPES = ("publish", "manage", "observe")
LADDER = ("observe", "manage", "publish")


def expand(scopes) -> list[str]:
    """The closure of ``scopes`` down the ladder, in SCOPES order; other scopes (``accounts``)
    pass through unchanged."""
    top = max((LADDER.index(s) for s in scopes if s in LADDER), default=-1)
    implied = [s for s in SCOPES if LADDER.index(s) <= top]
    return implied + [s for s in scopes if s not in LADDER]

# accounts is a *privileged operator* scope -- it mints/lists accounts, so it is NOT part of the
# per-account default set (an account admin must not be able to create other accounts). Only the
# root/bootstrap token (and tokens an operator explicitly issues it to) carries it.
ACCOUNT_ADMIN = "accounts"
ALL_SCOPES = (*SCOPES, ACCOUNT_ADMIN)
