# openmv-ota — build roadmap

Live list of what's being built next. Done work isn't tracked here (see git log).

## #4 Multi-tenancy (account scoping)

Products are namespaced by the maker's account: `(account_id, product_id)` is the real
identity. A `product_id` is unique only *within* an account.

**Foundation already in place** (so the rest can build on it):
- `account_id` threaded through the packages: `[product].account_id` → `system.json` →
  the trailer's JSON `meta` → the signed manifest body → the device check-in. Baked as
  `_ota_config.ACCOUNT_ID` for the installer's account guard. `''` = the implicit single
  account (self-host / pre-account devices).
- Device path scopes every release/rollout/pin lookup by `account_id`
  (`test_release_is_scoped_to_its_account` locks the isolation invariant).
- `Principal.account_id` is the admin-side seam (the account an admin credential acts for;
  the website injects it).
- `build factory-romfs` refuses an accountless golden unless `--no-account` (the golden is
  permanent — set the account before it's burned).

**Remaining slices:**

1. **Admin-side account scoping.** Resolve an admin credential → `Principal.account_id`,
   then filter *every* admin query (releases / rollouts / devices / fleet / audit /
   cohorts / pins) by it. Two accounts must never see each other's admin data.
2. **Account model + self-host account-creation endpoint.** An `accounts` table; a
   `POST` create-account verb returning `account_id` + a scoped admin token; product →
   account ownership registration (reject a `product_id` already owned by another account).
3. **Website identity seam.** The hosted path injects its own `admin_auth` that resolves
   a logged-in maker → `account_id` (billing/identity live in `openmv-ota-website`, not
   here). Self-host uses the built-in token store. The OTA server never holds billing.

## #4a Sticky device → account binding (the migration safety net)

**The problem it solves:** the golden BACK slot is immutable, so a device that falls back
to golden reports the golden's baked `account_id` (`''` or an old value). On a multi-tenant
server that strands it. We can't change the golden — so make the *server* remember the
account, so the device forgetting it on fallback stops mattering.

**Design:**
- **Learn:** on a check-in reporting a valid, non-empty `account_id`, record
  `device_id → account`.
- **Sticky:** a later check-in reporting `''` (or a different account) does **not**
  downgrade the binding.
- **Scope by the binding first**, falling back to the reported `account_id` when unbound.
- **Admin override is the authority** — a maker can force `device_id → account` server-side
  (also the fix for a mis-bind and the path for a legitimate re-account).

Effect: once a device has run a healthy FRONT under `acctY` even once, a later golden
fallback still resolves to `acctY` and gets offered the recovery update. The golden's baked
account becomes irrelevant for any device that's been healthy once.

**⚠️ This feature is security-adjacent — check-ins are unauthenticated (no device key), so
build it adversarially: try to break your own binding before shipping it.** Cases that must
be answered (add a test per row):

- **Griefer pre-binds** a known-registered `device_id` to an attacker account before the
  real owner checks in → real owner must be recoverable via admin override; blast radius
  must be only *offering* a wrong-account signed image, which won't install (signature
  gates it). Prove the signature still blocks the install.
- **Downgrade escape:** a device (or a spoofer) reports `''`/another account to slip its
  binding → sticky rule must hold; no cross-account release ever offered.
- **Cross-account offer:** exhaustively assert a bound device is never offered another
  account's release, via rollout, cohort, or pin.
- **Never-onboarded unit:** golden `''`, first FRONT boot fails → falls to golden →
  reports `''`, no prior binding → stranded. Document the mitigation (onboard through a
  `''`-serving server so it reports its account once, or admin pre-seed the batch); the
  factory rail is what prevents this for new products.
- **Registrar is not an account key:** `registrar_ref` is per-factory and shared across
  makers — it must never be used to infer the account (would cross-bind buyers of the same
  factory line).
- **Concurrency:** two accounts asserting the same `device_id` in the same window — define
  and test the deterministic winner (first valid assertion; admin override supersedes).
- **Table growth / DoS:** only registered devices can bind (the registration gate stands in
  front), so the binding table is bounded by the registered fleet — assert an unregistered
  id can never create a row.

When built, update `docs/threat-model.md` with the binding's trust assumptions (unauth
check-in claims, admin override as authority, signature as the real install gate).

## Cleanup (after #4)

- **#5 Fleet richness** — filtering/paging on the fleet + device views.
- **#6 `GET /admin/releases`** — list releases (currently no read endpoint).
- General admin API polish.
