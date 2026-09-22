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


# accounts is a *privileged operator* scope -- it mints/lists accounts, so it is NOT part of the
# per-account default set (an account admin must not be able to create other accounts). Only the
# root/bootstrap token (and tokens an operator explicitly issues it to) carries it.
ACCOUNT_ADMIN = "accounts"

# ...and `accounts.all` is the difference between the server's operator and a platform
# reselling the server. `accounts` provisions customers and manages the ones it
# provisioned; `accounts.all` sees and manages every account on the server, whoever made
# it. A partner gets the first and not the second, or its account directory is ours.
ACCOUNT_ROOT = "accounts.all"

ALL_SCOPES = (*SCOPES, ACCOUNT_ADMIN, ACCOUNT_ROOT)


def expand(scopes) -> list[str]:
    """The closure of ``scopes`` down the ladder, in SCOPES order; other scopes (``accounts``)
    pass through unchanged."""
    top = max((LADDER.index(s) for s in scopes if s in LADDER), default=-1)
    # the server's root sees every account: it reads what an observe token reads, across
    # all of them (the operator console's cross-account audit is such a read)
    if ACCOUNT_ROOT in scopes:
        top = max(top, LADDER.index("observe"))
    implied = [s for s in SCOPES if LADDER.index(s) <= top]
    out = implied + [s for s in scopes if s not in LADDER]
    # seeing every account implies being able to provision one: `accounts.all` is
    # `accounts` without the ownership filter, not a different job.
    if ACCOUNT_ROOT in out and ACCOUNT_ADMIN not in out:
        out.append(ACCOUNT_ADMIN)
    return out

