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
  ([The client](15-the-client.md)) is all a hosted maker ever does — everything on this
  page is the operator machinery behind that button.
- **Self-hosted:** `server init` prints a **bootstrap token** once at first setup
  ([The update server](20-update-server.md)) — every scope, acting for the implicit
  single account named `''`. A single-tenant self-host can `login` with it and publish,
  manage, and observe forever without ever creating an account: the verbs below exist
  for the **multi-tenant** case, carving real accounts out of one server and issuing
  each its own scoped tokens.


## Managing accounts

Every verb in this section **and** the token section below requires the privileged
`accounts` scope — and only an **operator's** credential carries it: the self-host
bootstrap token, or a token deliberately issued with that scope. An account's own
tokens never have it, so a tenant cannot create accounts or mint tokens, *not even
for itself*. That's deliberate: a stolen working token must not be able to
manufacture a replacement that survives revocation. (On the OpenMV-hosted service
these operations happen through the website, which holds the operator role.)

There are two operator scopes, because a server can have more than one operator:

- **`accounts`** provisions accounts and manages **the ones it created** — sees them in
  `list`, renames, limits, deactivates, mints their tokens — and cannot see that any
  other operator's account exists. This is what a partner reselling the service gets.
- **`accounts.all`** is the server's own root: it sees and manages every account,
  whoever made it. The bootstrap token has it. Nobody outside the operator of the
  server should.

An account is filed under the credential that created it, and account names are unique
among that operator's accounts rather than across the server — two operators can each
have a customer called "DroneCo".

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

A script that creates accounts should pass **`--client-ref`** — its own id for the
account, a customer or workspace id in whatever system is driving this. It makes the
call safe to retry: asking again with the same reference returns the account already
made, with `created: false` and no token, instead of a second account or a 409 you
cannot tell apart from the name being taken by someone else:

```
$ openmv-ota client account create --name "DroneCo" --client-ref cust_8a41f2 --json
{"account_id": "acct_7bd21c50e83a94f1", "name": "DroneCo", "token": "5oQ4wLr8kJ2vN9xB1mA3sT6yD0eF7cH_gPzUiRnE2aM", "created": true, "client_ref": "cust_8a41f2"}
$ openmv-ota client account create --name "DroneCo" --client-ref cust_8a41f2
account acct_7bd21c50e83a94f1 already exists for that --client-ref
```

`list` shows the accounts your credential provisioned (every account, under
`accounts.all`), each with what a directory shows beside a name — registered devices,
releases, active rollouts, the newest check-in. `--q` searches by name, id or client
reference, `--active`/`--inactive` narrow to switched-on or deactivated accounts,
`--limit`/`--offset` page, and `total` counts what the filters matched. `show` is one
account's row by id, counts included, without listing the rest; `rename` changes only
the display name (the id is forever):

```
$ openmv-ota client account list --q drone
{
  "accounts": [
    {
      "account_id": "acct_7bd21c50e83a94f1",
      "name": "DroneCo",
      "created_at": "2026-08-31T20:02:17.481903+00:00",
      "active": 1,
      "device_limit": 10,
      "client_ref": "cust_8a41f2",
      "devices": 7,
      "releases": 3,
      "active_rollouts": 1,
      "last_seen": "2026-09-20T14:02:11.318557+00:00"
    }
  ],
  "total": 1
}

$ openmv-ota client account show --account-id acct_7bd21c50e83a94f1
{
  "account_id": "acct_7bd21c50e83a94f1",
  "name": "DroneCo",
  "created_at": "2026-08-31T20:02:17.481903+00:00",
  "active": 1,
  "device_limit": 10,
  "client_ref": "cust_8a41f2",
  "devices": 7,
  "releases": 3,
  "active_rollouts": 1,
  "last_seen": "2026-09-20T14:02:11.318557+00:00"
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

### Device limits

An account can carry a device entitlement — the most devices it may register:

```
$ openmv-ota client account limit --account-id acct_7bd21c50e83a94f1 --devices 10
account acct_7bd21c50e83a94f1 device limit: 10 (7 registered)
$ openmv-ota client account limit --account-id acct_7bd21c50e83a94f1 --unlimited
```

It is enforced for *new* devices at check-in: past the limit, an unknown device is
not registered and is served nothing (zero footprint, exactly like an unregistered
id), and a `device.refused` audit row records it once per device id. Devices already
in the fleet are never affected — they keep checking in and updating. This is the
hook a hosting layer uses to map a plan onto an account.

## Tokens and scopes

A token acts for one account and carries **scopes**, which form a ladder — each rung
includes the ones below it:

- `observe` — read everything
- `manage` — observe, plus rollouts, cohorts, pins, binds
- `publish` — manage, plus publishing releases

Name the highest rung you need; the server fills in the rest (a `publish` token is
stored, listed, and checked as `publish, manage, observe`). `accounts` is the separate
operator scope. `issue` defaults to the top rung; give a CI machine only what it needs:

```
$ openmv-ota client token issue --account-id acct_7bd21c50e83a94f1 --name ci --scope publish
token 3f2a9c1e77d0b4a8 issued for acct_7bd21c50e83a94f1
token (store it now -- not recoverable): xK9pW2qL5mR8tV1zC4nB7dF0gJ3hS6yA_eU2iO5rT8wQ
```

A token can also be limited to some of the account's **products** when it is issued.
`--scope` says what it may do; `--product-id` (repeatable) says what it may do it to:

```
$ openmv-ota client token issue --account-id acct_7bd21c50e83a94f1 --name partner \
      --scope observe --product-id 5553380507785669254
```

That credential sees those products' releases, devices, rollouts and audit history and
nothing else in the account — not the other products, and not the account-level record
of tokens and limits. It is how you hand a customer a view of their own line without a
view of the fleet.

A name is unique among an account's live tokens (it is what the audit log records as
the actor); revoking a token frees its name. The secret is shown **once** — the server
stores only a hash, so `list` shows metadata and hashes, never secrets. The hash is the
id you revoke or rotate by:

```
$ openmv-ota client token list --account-id acct_7bd21c50e83a94f1
{
  "tokens": [
    {
      "token_hash": "3f2a9c1e77d0b4a8c5e2f91d6b038a7c4d1e8f25a9c6b3d07e4f1a852c9b6e03",
      "name": "ci",
      "scopes": ["publish", "manage", "observe"],
      "products": [],
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
$ openmv-ota client device bind --device-id OPENMV_N6:30003d000851303436313832
device OPENMV_N6:30003d000851303436313832 bound to acct_7bd21c50e83a94f1
```

A bind works **before the camera has ever checked in** — it is recorded against the id,
not on a fleet row — so hardware can be assigned to an account when it ships and its
first check-in lands where you expect.

Knowing a device id is not owning the device: a binding only controls visibility and
offers, never installs — the camera verifies every image against the keys baked into
its own firmware, so another account's releases can't run on it. An admin-bound device
can't be re-bound by another account (their attempt reads as not-found), and your admin
bind always recovers a wrongly learned one. On the OpenMV-hosted service, who may bind
a given device is additionally gated by proof of ownership; what this layer deliberately
does *not* guarantee is listed in [residual threats](../compliance/residual-threats.md).

The other half of binding is ending it. When a unit is retired:

```
$ openmv-ota client device forget --device-id OPENMV_N6:30003d000851303436313832
device OPENMV_N6:30003d000851303436313832 removed from the fleet
```

It leaves the fleet and stops counting against the device limit, and everything it
stored in the datalake — telemetry, logs, frames — is erased with it, first: a datalake
that cannot be reached answers 502 and leaves the device in place to retry, rather than
forgetting it with its data orphaned. Pass `--keep-data` to leave the data to the
datalake's retention instead. Its install history stays — a deployment row records what
happened on a day that has already passed, and rollout counters are built from those
rows — and the removal is in the audit log, with what was erased. A camera that checks in
again afterwards is a device the server has not seen before: it enrols from scratch, and
is not yours again until you bind it.

---

*[← 18 · Building deltas](18-building-deltas.md) · [Index](00-introduction.md) · [20 · The update server →](20-update-server.md)*
