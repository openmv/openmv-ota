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

## Done (cleanup of the loose ends)

- **HTTP account-creation endpoint** — `POST /admin/accounts` + `GET /admin/accounts` behind a new
  privileged `account:admin` scope (held by the bootstrap/root token, not a regular account's).
  `client account create/list`. The self-host CLI (`server account create`) still works.
- **Admin-bind row sync** — `POST /devices/{id}/account` now syncs the `devices` row's account
  inline, so fleet views update immediately instead of on the next check-in.
- **Pagination** — `--limit`/`--offset` on the releases + rollouts listings (consistent with the
  device view).

## Done during the API audit

- **Scope naming** flattened to `publish`/`manage`/`observe`/`accounts`.
- **`''` account** stays a sentinel (no row), rendered `(unassigned)` by the tools.
- **Token management surface** — full lifecycle over the API, all `accounts`-scoped so a stolen
  worker token can't mint a revocation-surviving token: `POST/GET /accounts/{id}/tokens`,
  `POST /tokens/{hash}/revoke`, `POST /tokens/{hash}/rotate` (issue-fresh + revoke-old). Secret
  returned only at issue/rotate; `list_tokens` carries + filters by account; `server token list`
  shows the account; `server token rotate`; client `token issue/list/revoke/rotate`.
  Security posture chosen: **show-once everywhere, no re-display** (the website shows once + stores
  a reference, not the secret) — reversing the earlier "view any time" idea.
- **Account lifecycle** — `PATCH /accounts/{id}` (rename), `POST /accounts/{id}/deactivate`
  (revoke all tokens + `active=0`; soft — fielded devices keep being served), `.../activate`.
  Minting into a deactivated account (issue/rotate) → 409. Client + server-CLI parity.

## Remaining / optional

- Consistent error envelopes (FastAPI's `{detail}` is the current shape).
- Account-scoped read indexes lag multi-tenancy (indexes lead with `product_id`, not `account_id`);
  additive index-only migration when fleets/accounts grow.
- Anything else surfaced during the section-by-section API audit.
