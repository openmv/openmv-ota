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

## Done (pluggable signer + enforced key hygiene — CRA-grade)

- **`Signer` abstraction** (`ota/signer.py`) — `build_signer` factory dispatching on
  `keys/backends.json`; `sign()` returns raw `R||S`, `public_point_hex()` feeds the build-time
  consistency check. Backends: encrypted-PEM floor, PKCS#11 (any HSM/token), AWS/GCP/Azure KMS,
  `custom` dotted-path hook. Heavy SDKs behind pip extras (`hsm`/`aws-kms`/`gcp-kms`/`azure-kms`)
  guarded by `ota/_extras.py`; real hardware/cloud paths `pragma: no cover`, logic fully tested via
  injectable `session=`/`client=` seams.
- **No plaintext, ever** — `private_key_pem` is encrypt-only (`BestAvailableEncryption`); no
  `NoEncryption` write path exists. `project new --dev` mints a random passphrase in gitignored
  `keys/.dev-passphrase`; non-dev requires `--key-passphrase-file`/env/prompt, never written.
  `project keys encrypt` migrates legacy plaintext projects.
- **Dev-key rail** — `Signer.is_dev_key` set *structurally* (passphrase came from `.dev-passphrase`);
  the production build refuses a dev key unless `--allow-dev-key` (per-build, loud, audited).
- **Dev provenance** — `"dev": true` stamped into the *signed* system.json + trailer + manifest;
  `releases.dev` column; server serves + surfaces it (visibility only, never a gate — the whole
  pipeline stays testable with dev keys).
- **External key provisioning** — `KeyProvisioner` ABC + `provision_external_key_set`; PKCS#11
  `C_GenerateKeyPair` / AWS `CreateKey` / GCP `CreateCryptoKeyVersion` / Azure `create_ec_key`
  (`pragma: no cover`). `project keys backend show|configure|provision`: `configure` points a
  trusted key at an externally-held key (bring-your-own-key); `provision` mints a fresh pool
  in-token/KMS and re-keys — no private PEM ever on disk. `backends.json` is committed (non-secret).

## Remaining / optional

- **Real-hardware/cloud acceptance** for the signer backends (SoftHSM opt-in test exists; AWS/GCP/
  Azure + provisioning are unit-covered via fakes but need one live end-to-end pass each).
- **KMS bulk-provisioning cost** — `keys backend provision` defaults to a small pool (1 factory +
  4 ota) because external keys are billable; document per-provider pricing before recommending it.
- `configure` with an explicit `DIR` must place it *before* `--set`/`--backend` (argparse can't
  split two positionals across value options); the default `.` covers the common in-project case.

- Consistent error envelopes (FastAPI's `{detail}` is the current shape).
- Account-scoped read indexes lag multi-tenancy (indexes lead with `product_id`, not `account_id`);
  additive index-only migration when fleets/accounts grow.
- Anything else surfaced during the section-by-section API audit.
