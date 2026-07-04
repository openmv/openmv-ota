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

## Surfaced during the API audit

- **Token-issue API endpoint + rotation.** Today `POST /admin/accounts` mints an account's *first*
  token, and issuing/revoking *further* tokens is CLI-only (`server token issue --account` /
  `server token revoke`), which needs host access. The cloud website can't add or replace a maker's
  token over the API. Add:
  - an endpoint to **issue an additional token** for an account (so a maker/website can have >1, or
    replace one), and
  - **rotation** = issue-fresh + revoke-old over the API.
  Context: we chose the cloud model where the website provisions via the API and lets the maker
  **view their token any time** (website keeps a retrievable copy; the server still stores only the
  hash). That makes rotation a *convenience* for the leaked-token case, not required for a lost
  token — so this is wanted but not a blocker.
- **Scope naming** flattened to `publish`/`manage`/`observe`/`accounts` (done).
- **`''` account** stays a sentinel (no row), rendered `(unassigned)` by the tools (done).

## Remaining / optional

- Consistent error envelopes (FastAPI's `{detail}` is the current shape).
- Account-scoped read indexes lag multi-tenancy (indexes lead with `product_id`, not `account_id`);
  additive index-only migration when fleets/accounts grow.
- Anything else surfaced during the section-by-section API audit.
