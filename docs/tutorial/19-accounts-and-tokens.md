# Accounts and tokens

*[← 18 · Building deltas](18-building-deltas.md) · [Index](00-introduction.md) · [20 · The update server →](20-update-server.md)*

---

Everything on the previous pages happened inside one **account**: yours. The account
is the tenancy boundary — releases, rollouts, cohorts, devices, and the audit log are
all namespaced by it, and one tenant can never see or touch another's (a cross-account
lookup reads as not-found, so probing reveals nothing). This page is the layer itself:
where credentials come from, the verbs that manage them, and how a device ends up
belonging to an account.

## Where your first token comes from

- **OpenMV-hosted (the default):** your account was created at sign-up, and the
  website issues (and revokes) your working tokens. Logging in with one
  ([page 15](15-the-client.md)) is all a hosted maker ever does — everything on this
  page is the operator machinery behind that button.
- **Self-hosted:** `server init` prints a **bootstrap token** once at first setup
  ([page 20](20-update-server.md)) — all four scopes, acting for the implicit single
  account named `''`. A single-tenant self-host can `login` with it and publish,
  manage, and observe forever without ever creating an account: the verbs below exist
  for the **multi-tenant** case, carving real accounts out of one server and issuing
  each its own scoped tokens.


## Managing accounts

Every verb in this section **and** the token section below requires the privileged
`accounts` scope — and only the **operator's** credential carries it: the self-host
bootstrap token, or a token deliberately issued with that scope. An account's own
tokens never have it, so a tenant cannot create accounts or mint tokens, *not even
for itself*. That's deliberate: a stolen working token must not be able to
manufacture a replacement that survives revocation. (On the OpenMV-hosted service
these operations happen through the website, which holds the operator role.)

A brand-new account has no credentials yet, so `create` returns two things: the
account id, and the account's **first working token** (scopes publish, manage,
observe — everything a tenant does day to day, and nothing operator-level). It is
displayed only in this one response — the server stores just its hash — so capture it
now; every later token for the account comes from `token issue` below:

```
$ openmv-ota client account create --name "DroneCo"
account acct_7bd21c50e83a94f1 created
working token (store it now -- not recoverable): 5oQ4wLr8kJ2vN9xB1mA3sT6yD0eF7cH_gPzUiRnE2aM
```

`list` shows every account; `rename` changes only the display name (the id is
forever):

```
$ openmv-ota client account list
{
  "accounts": [
    {
      "account_id": "acct_7bd21c50e83a94f1",
      "name": "DroneCo",
      "created_at": "2026-08-31T20:02:17.481903+00:00",
      "active": 1
    }
  ]
}

$ openmv-ota client account rename --account-id acct_7bd21c50e83a94f1 --name "DroneCo GmbH"
account acct_7bd21c50e83a94f1 renamed to DroneCo GmbH
```

`deactivate` is the soft off-switch: it revokes every token and blocks minting new
ones, so admin access dies — but fielded devices keep being served, so a billing
lapse never bricks a fleet. `activate` re-enables the account; the old tokens stay
revoked, so issue fresh ones after:

```
$ openmv-ota client account deactivate --account-id acct_7bd21c50e83a94f1
account acct_7bd21c50e83a94f1 deactivated (3 token(s) revoked)

$ openmv-ota client account activate --account-id acct_7bd21c50e83a94f1
account acct_7bd21c50e83a94f1 activated
```

## Tokens and scopes

A token acts for one account and carries **scopes** — `publish` (publish releases),
`manage` (rollouts, cohorts, pins, binds), `observe` (read everything), and the
operator scope `accounts`. `issue` defaults to the worker set (publish, manage,
observe); give a CI machine only what it needs:

```
$ openmv-ota client token issue --account-id acct_7bd21c50e83a94f1 --name ci --scope publish
token 3f2a9c1e77d0b4a8 issued for acct_7bd21c50e83a94f1
token (store it now -- not recoverable): xK9pW2qL5mR8tV1zC4nB7dF0gJ3hS6yA_eU2iO5rT8wQ
```

The secret is shown **once** — the server stores only a hash, so `list` shows
metadata and hashes, never secrets. The hash is the id you revoke or rotate by:

```
$ openmv-ota client token list --account-id acct_7bd21c50e83a94f1
{
  "tokens": [
    {
      "token_hash": "3f2a9c1e77d0b4a8c5e2f91d6b038a7c4d1e8f25a9c6b3d07e4f1a852c9b6e03",
      "name": "ci",
      "scopes": ["publish"],
      "account_id": "acct_7bd21c50e83a94f1",
      "created_at": "2026-08-31T20:05:44.190226+00:00",
      "revoked": 0
    }
  ]
}

$ openmv-ota client token revoke <token-hash>
$ openmv-ota client token rotate <token-hash>         # replacement issued, old revoked
```

## How a device knows its account

It's baked in at build: you put your account id in
the project (`account_id` under `[product]` in `openmv-ota.toml`), the build stamps it
into the image's `system.json`, and the device reports it with every check-in. On the
first valid check-in the server **learns** that binding and it's sticky from then on —
a later boot reporting a different or empty account (a factory-state fallback, say)
can't move the device. The operator override — the recovery path when a camera was
first seen under the wrong account — (re)binds it to **yours**:

```
$ openmv-ota client device bind --device-id 30003d000851303436313832
device 30003d000851303436313832 bound to acct_7bd21c50e83a94f1
```

Knowing a device id is not owning the device: a binding only controls visibility and
offers, never installs — the camera verifies every image against the keys baked into
its own firmware, so another account's releases can't run on it. An admin-bound device
can't be re-bound by another account (their attempt reads as not-found), and your admin
bind always recovers a wrongly learned one. On the OpenMV-hosted service, who may bind
a given device is additionally gated by proof of ownership; what this layer deliberately
does *not* guarantee is listed in [residual threats](../compliance/residual-threats.md).

---

*[← 18 · Building deltas](18-building-deltas.md) · [Index](00-introduction.md) · [20 · The update server →](20-update-server.md)*
