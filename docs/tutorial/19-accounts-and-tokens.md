# Accounts and tokens

*[← 18 · Building deltas](18-building-deltas.md) · [Index](00-introduction.md) · [20 · The update server →](20-update-server.md)*

---

Everything on the previous pages happened inside one **account**: yours. The account
is the tenancy boundary — releases, rollouts, cohorts, devices, and the audit log are
all namespaced by it, and one tenant can never see or touch another's (a cross-account
lookup reads as not-found, so probing reveals nothing). On the OpenMV-hosted service
an account came with your sign-up; the token you `login` with acts for it. This page
is the layer itself: the credentials that act for an account, and how a device ends up
belonging to one.


## Managing accounts and their tokens

Two verbs manage the layer; both need the privileged `accounts` scope, which ordinary
working tokens don't carry (on the OpenMV-hosted service, account management happens
through your OpenMV account — these verbs are the same operations, exposed to
operators and self-hosts):

```
$ openmv-ota client account create --name "DroneCo"
account acct_7bd21c50e83a94f1 created
admin token (store it now -- not recoverable): 5oQ4wLr8kJ2vN9xB1mA3sT6yD0eF7cH_gPzUiRnE2aM

$ openmv-ota client account list
$ openmv-ota client account rename --account-id acct_7bd21c50e83a94f1 --name "DroneCo GmbH"
account acct_7bd21c50e83a94f1 renamed to DroneCo GmbH

$ openmv-ota client account deactivate --account-id acct_7bd21c50e83a94f1
account acct_7bd21c50e83a94f1 deactivated (3 token(s) revoked)
$ openmv-ota client account activate --account-id acct_7bd21c50e83a94f1
account acct_7bd21c50e83a94f1 activated

$ openmv-ota client token issue --account-id acct_7bd21c50e83a94f1 --name ci --scope publish
token 3f2a9c1e77d0b4a8 issued for acct_7bd21c50e83a94f1
token (store it now -- not recoverable): xK9pW2qL5mR8tV1zC4nB7dF0gJ3hS6yA_eU2iO5rT8wQ

$ openmv-ota client token list --account-id acct_7bd21c50e83a94f1
$ openmv-ota client token revoke <token-hash>
$ openmv-ota client token rotate <token-hash>         # replacement issued, old revoked
```

| about tokens | |
|---|---|
| scopes | `publish` (publish releases), `manage` (rollouts, cohorts, pins, binds), `observe` (read everything), `accounts` (the operator scope). `token issue` defaults to the worker set: publish, manage, observe |
| secrets | shown **once**, at issue/rotate — the server stores only a hash. `token list` shows metadata and hashes, never secrets |
| revocation | by hash. `deactivate` revokes every token an account has and blocks issuing new ones — admin access dies, but fielded devices keep being served, so a billing lapse never bricks a fleet |

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
a given device is additionally gated by proof of ownership; the
[threat model](../reference/threat-model.md) spells out the full trust story.

---

*[← 18 · Building deltas](18-building-deltas.md) · [Index](00-introduction.md) · [20 · The update server →](20-update-server.md)*
