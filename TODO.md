# openmv-ota — build roadmap

Live list of what's being built next. Done work isn't tracked here (see git log).

## Done (multi-tenancy + cleanup — see git log for detail)

- **#4 account scoping** — account model (`accounts`, tokens carry an account, `Principal.account_id`),
  every admin read/write scoped to the token's account (404 across accounts), publish enforced to
  your own account, per-account anti-rollback + audit; `server account create/list`; the injected
  `create_app(admin_auth=…)` website identity seam (documented in `docs/server.md`).
- **#4a sticky device→account binding** — learned/sticky/admin-override; the golden-fallback
  migration safety net, built adversarially (trust model in `docs/threat-model.md`).
- **#5 fleet richness** — cohort filter + `--limit`/`--offset` paging on the device view.
- **#6 `GET /admin/releases`** — account-scoped release listing + `client releases`.

## Remaining / optional

- **HTTP account-creation endpoint.** Self-host creates accounts via the CLI (`server account
  create`). A `POST /admin/accounts` for the *hosted* flow would need a super-admin scope (an
  account that may mint accounts); in practice the website mediates account creation via its own
  identity, so this is only needed if a programmatic self-host API is wanted. Not built.
- **Admin-bind row sync.** `POST /devices/{id}/account` sets the binding immediately but the
  `devices` row's `account_id` (what `list_devices` filters on) syncs on the device's *next*
  check-in — eventually consistent. Sync the row inline if the admin-visibility lag matters.
- General admin API polish (consistent error envelopes, list pagination on releases/rollouts).
