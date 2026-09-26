"""Metadata store -- releases, rollouts, the device registry, admin tokens, the audit log.

One SQL implementation over a DBAPI connection, subclassed for **sqlite** (dev/test) and
**postgres** (prod); they differ only in how they connect and the ``?`` vs ``%s`` parameter
style. The schema is created by **versioned migrations** tracked in a ``meta`` table; feature
tables are added by later migrations as each feature lands. Rows come back keyed by column name
(``sqlite3.Row`` / psycopg ``dict_row``). A lock serializes access to the single connection --
fine for the MVP; a pool is a scale-time concern.

The store is duck-typed (no strict ABC) so OpenMV's website can inject a custom implementation
via ``create_app(metastore=...)``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import time
import threading
from datetime import datetime, timedelta, timezone

from .errors import ServerError


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _d(row) -> dict | None:
    """A store row as a dict, with ``product_id_str`` beside any ``product_id``.

    A product id is 63 bits, and JSON numbers are IEEE doubles in JavaScript: anything
    above 2**53 loses precision the moment a JS or TS client calls ``JSON.parse``, and
    it loses it SILENTLY -- the id comes back rounded and every lookup with it misses.
    Python and MicroPython are fine, so nothing in this stack notices; an integrator's
    Node service would. The string is the exact value, always safe to read.

    Added here rather than in each handler so it cannot be added to nine responses and
    forgotten on the tenth."""
    if row is None:
        return None
    d = dict(row)
    if "product_id" in d and d["product_id"] is not None:
        d["product_id_str"] = str(d["product_id"])
    return d


def _order(sort, direction, allowed: dict, default: str, tiebreak: str) -> str:
    """An ``ORDER BY`` from a whitelisted sort key. ``allowed`` maps public keys to SQL
    expressions; an unknown key falls back to ``default`` (the list's natural order),
    so user input never reaches the SQL. ``tiebreak`` keeps pages stable."""
    if sort in allowed:
        d = "DESC" if str(direction).lower() == "desc" else "ASC"
        return " ORDER BY %s %s, %s" % (allowed[sort], d, tiebreak)
    return " ORDER BY " + default


def _iso_at(epoch: float) -> str:
    """An epoch as the ISO-8601 UTC string the store keeps timestamps in (so string
    comparison in SQL orders by time)."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def _limit(sql: str, params: tuple, limit, offset: int) -> tuple[str, tuple]:
    """Append the page clause. A limit pages; an offset alone still skips rows
    (SQLite reads ``LIMIT -1`` as no cap); neither leaves the query whole."""
    if limit is not None:
        return sql + " LIMIT ? OFFSET ?", (*params, limit, offset)
    if offset:
        return sql + " LIMIT -1 OFFSET ?", (*params, offset)
    return sql, params


def _and(where: str, clause: str) -> str:
    return (where + " AND " + clause) if where else "WHERE " + clause


def _scope(account_id=None, product_id=None, products=None) -> tuple[str, tuple]:
    """A ``WHERE`` clause + params for the optional (account_id, product_id) filters -- the
    building block for account-scoped admin reads. Either/both may be None (no filter).

    ``products`` is a credential's product allow-list, and it is a different thing from
    ``product_id``: that is a caller ASKING for one product, this is a token being
    ALLOWED only some. ``None`` is an ordinary token (the whole account); a list narrows
    every read to those ids. An empty list is a token scoped to nothing and must see
    nothing -- written out because ``IN ()`` is a syntax error, and the tempting shortcut
    of skipping the clause turns "allowed nothing" into "allowed everything"."""
    conds, params = [], []
    if account_id is not None:
        conds.append("account_id = ?")
        params.append(account_id)
    if product_id is not None:
        conds.append("product_id = ?")
        params.append(product_id)
    if products is not None:
        if products:
            conds.append("product_id IN (%s)" % ",".join(["?"] * len(products)))
            params.extend(products)
        else:
            conds.append("1 = 0")
    return (("WHERE " + " AND ".join(conds)) if conds else ""), tuple(params)


def _audit_hash(prev: str, ts: str, actor: str, action: str, etype: str, eid: str,
                payload: str) -> str:
    return hashlib.sha256(
        "|".join((prev, ts, actor or "", action, etype or "", eid or "", payload)).encode()
    ).hexdigest()


# Each entry is a list of DDL statements; its 1-based index is the schema version it defines.
_MIGRATIONS: list[list[str]] = [
    [   # v1 -- the MVP feature tables. Everything groups by product_id (int): the manifest carries
        # product_id (not a camera-model string), and the device check-in sends the same value, so
        # it's the reliable release<->device join. product/board are display-only.
        """CREATE TABLE releases (
            release_id TEXT PRIMARY KEY, product_id BIGINT NOT NULL, product TEXT,
            version TEXT NOT NULL, payload_version INTEGER NOT NULL,
            min_platform_version INTEGER NOT NULL DEFAULT 0,
            image_sha256 TEXT NOT NULL, image_size INTEGER NOT NULL, representations TEXT NOT NULL,
            manifest_key TEXT NOT NULL, image_key TEXT NOT NULL, delta_key TEXT,  -- delta_key: unused since a release carries N deltas, keyed by name
            key_id INTEGER, uploaded_by TEXT, uploaded_at TEXT NOT NULL)""",
        """CREATE TABLE rollouts (
            rollout_id TEXT PRIMARY KEY, release_id TEXT NOT NULL, product_id BIGINT NOT NULL,
            cohort TEXT NOT NULL, percent REAL NOT NULL, state TEXT NOT NULL,
            failure_threshold REAL NOT NULL DEFAULT 0.05, attempted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE devices (
            device_id TEXT PRIMARY KEY, product_id BIGINT NOT NULL, board TEXT,
            cohort TEXT NOT NULL DEFAULT '__default__', current_version TEXT,
            current_payload_version INTEGER, slot TEXT, representation TEXT, fallback_reason TEXT,
            confirmed INTEGER, last_offered_release_id TEXT, owner_ref TEXT,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)""",
        """CREATE TABLE admin_tokens (
            token_hash TEXT PRIMARY KEY, name TEXT NOT NULL, scopes TEXT NOT NULL,
            created_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE audit (
            seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT, action TEXT NOT NULL,
            entity_type TEXT, entity_id TEXT, data TEXT NOT NULL, prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL)""",
        "CREATE INDEX idx_rollouts_board_cohort ON rollouts (product_id, cohort, state)",
        "CREATE INDEX idx_devices_board ON devices (product_id)",
        "CREATE INDEX idx_releases_board ON releases (product_id, payload_version)",
    ],
    [   # v2 -- explicit device->server outcome reports (POST /feedback). One authoritative row per
        # (device_id, release_id); bounded by the registered fleet x releases, so still zero-footprint.
        """CREATE TABLE deployments (
            device_id TEXT NOT NULL, release_id TEXT NOT NULL, product_id BIGINT NOT NULL,
            status TEXT NOT NULL, reason TEXT, reported_at TEXT NOT NULL,
            PRIMARY KEY (device_id, release_id))""",
        "CREATE INDEX idx_deployments_release ON deployments (release_id, status)",
    ],
    [   # v3 -- version pins: force a specific device or cohort onto a release, overriding rollouts.
        "ALTER TABLE devices ADD COLUMN pinned_release_id TEXT",
        """CREATE TABLE cohort_pins (
            product_id BIGINT NOT NULL, cohort TEXT NOT NULL, release_id TEXT NOT NULL,
            PRIMARY KEY (product_id, cohort))""",
    ],
    [   # v4 -- account scoping: a product_id is unique only *within* a maker's account, so
        # (account_id, product_id) is the real identity. account_id rides in the manifest JSON +
        # the check-in; '' is the implicit single account (self-host / pre-account devices). The
        # device path scopes every release/rollout/pin lookup by it, so two accounts that happen
        # to share a product_id never see each other's firmware. cohort_pins is rebuilt to put
        # account_id in the key.
        "ALTER TABLE releases ADD COLUMN account_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE rollouts ADD COLUMN account_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE devices ADD COLUMN account_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE deployments ADD COLUMN account_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE cohort_pins RENAME TO cohort_pins_v3",
        """CREATE TABLE cohort_pins (
            account_id TEXT NOT NULL DEFAULT '', product_id BIGINT NOT NULL, cohort TEXT NOT NULL,
            release_id TEXT NOT NULL, PRIMARY KEY (account_id, product_id, cohort))""",
        "INSERT INTO cohort_pins (product_id, cohort, release_id) "
        "SELECT product_id, cohort, release_id FROM cohort_pins_v3",
        "DROP TABLE cohort_pins_v3",
    ],
    [   # v5 -- rename devices.owner_ref -> registrar_ref: it holds sha256(form_key), i.e. the
        # party that *registered* the unit (a factory / form-key holder), not who owns it. Both
        # sqlite (>=3.25) and postgres support RENAME COLUMN.
        "ALTER TABLE devices RENAME COLUMN owner_ref TO registrar_ref",
    ],
    [   # v6 -- accounts (multi-tenancy): an admin credential belongs to an account, and every
        # admin read is scoped to it. '' is the implicit single account (self-host bootstrap
        # token + pre-account data), so an un-migrated self-host keeps seeing everything.
        """CREATE TABLE accounts (
            account_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL)""",
        "ALTER TABLE admin_tokens ADD COLUMN account_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE audit ADD COLUMN account_id TEXT NOT NULL DEFAULT ''",
    ],
    [   # v7 -- sticky device->account binding: the authoritative account for a device, so a golden
        # fallback (which reports the golden's baked account, maybe '') can't strand a device that
        # was healthy under a real account. 'learned' from the first valid check-in (sticky -- never
        # downgraded), or 'admin' (an operator override). Only registered devices ever reach the
        # bind path, so this table is bounded by the registered fleet.
        """CREATE TABLE device_accounts (
            device_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, source TEXT NOT NULL,
            bound_at TEXT NOT NULL)""",
    ],
    [   # v8 -- account deactivation: a soft on/off flag. Deactivate = revoke all the account's
        # tokens + set active=0 (admin access dies; fielded devices keep being served, so a billing
        # lapse never bricks a fleet). No new token can be issued/rotated for an inactive account.
        "ALTER TABLE accounts ADD COLUMN active INTEGER NOT NULL DEFAULT 1",
    ],
    [   # v9 -- dev-signed provenance: a release built with a throwaway --dev key carries dev=1 (read
        # from the signed manifest). Visibility only -- never a gate -- so operators can spot a dev
        # image published to a real fleet.
        "ALTER TABLE releases ADD COLUMN dev INTEGER NOT NULL DEFAULT 0",
    ],
    [   # v10 -- the live image streams a device reported at its last check-in, comma-separated.
        # The camera grant already uses them; persisting lets a *viewer* grant enumerate a device's
        # panes without waiting for the device to be online, which is what a dashboard needs.
        "ALTER TABLE devices ADD COLUMN streams TEXT NOT NULL DEFAULT ''",
    ],
    [   # v11 -- what the device would fall back to (A/B). The columns above describe the image
        # that is RUNNING; an operator watching a rollout needs the other half: a fleet where every
        # device's fallback is the previous release is in a very different position from one where
        # half the devices have no second image at all. NULL means the device did not say --
        # a single-image board, or a payload from before the slots field existed -- which is
        # deliberately distinct from "reported no fallback".
        "ALTER TABLE devices ADD COLUMN fallback_payload_version INTEGER",
    ],
    [   # v12 -- the RUNNING slot's exact bytes, named: the trailer's body_sha256 as reported in
        # the check-in slots list. A version stopped being a byte identity when --allow-republish
        # arrived, and a delta base matches by version AND sha -- so "which delta bases must this
        # release cover?" (GET /fleet/bases, `client release bases --fleet`) needs the sha, not
        # just the version. NULL means the device did not say (a pre-sha payload).
        "ALTER TABLE devices ADD COLUMN body_sha256 TEXT",
    ],
    [   # v13 -- the release's SBOM (CycloneDX JSON), uploaded at publish beside the artifacts:
        # the dependency evidence for the exact bytes a fleet runs, served per release
        # (GET /releases/{id}/sbom) instead of living only on the build machine. NULL = the
        # release was published without one (an older client).
        "ALTER TABLE releases ADD COLUMN sbom_key TEXT",
    ],
    [   # v14 -- a human-facing display name for a device, set by the operator (the website's
        # rename button / `client device rename`). Pure label: never used for lookups, offers,
        # or identity -- the device_id remains the only key a device ever reports.
        "ALTER TABLE devices ADD COLUMN display_name TEXT NOT NULL DEFAULT ''",
    ],
    [   # v15 -- the same operator-facing display name for releases and rollouts (set at
        # publish/create time or renamed later). Labels only, need not be unique; the
        # rel_/ro_ ids remain the identity everywhere.
        "ALTER TABLE releases ADD COLUMN display_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE rollouts ADD COLUMN display_name TEXT NOT NULL DEFAULT ''",
    ],
    [   # v16 -- CVE monitoring: findings from scanning stored SBOMs against a vulnerability
        # database (OSV). One row per (release, advisory, component); a row whose advisory a
        # later scan no longer reports is CLEARED, never deleted -- the history is the
        # CRA-facing evidence that monitoring ran and what it found.
        """CREATE TABLE advisories (
            release_id TEXT NOT NULL,
            vuln_id TEXT NOT NULL,
            component TEXT NOT NULL,
            version TEXT NOT NULL DEFAULT '',
            severity TEXT NOT NULL DEFAULT 'unknown',
            summary TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            cleared_at TEXT,
            account_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (release_id, vuln_id, component)
        )""",
        "CREATE INDEX idx_advisories_account ON advisories (account_id, cleared_at)",
    ],
    [   # v17 -- cohorts become first-class: a declared label can exist with NO devices in it
        # (created ahead of its first assign). The list is the union of declared labels and
        # the labels found on device rows, so `assign` still springs a cohort into being
        # implicitly (it auto-declares) and a declaration is never required to use one.
        """CREATE TABLE cohorts (
            account_id TEXT NOT NULL DEFAULT '',
            cohort TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (account_id, cohort)
        )""",
    ],
    [   # v18 -- per-account device limit (entitlement). NULL = unlimited. Enforced at
        # check-in for NEW devices only: a fleet that has grown past its plan keeps every
        # device it already has (they keep checking in and updating); the (limit+1)th device
        # is simply not registered -- zero footprint, same as an unregistered id -- and the
        # refusal is audited once per device id so the operator can see it.
        "ALTER TABLE accounts ADD COLUMN device_limit INTEGER",
    ],
    [   # v19 -- a product's own display name. Products are not entities of their own
        # (an id seen on devices and releases); this is the one thing an operator SETS
        # about one, so it gets a row. Empty/no row = the newest release's manifest name.
        "CREATE TABLE IF NOT EXISTS products ("
        "account_id TEXT NOT NULL DEFAULT '', product_id BIGINT NOT NULL, "
        "display_name TEXT NOT NULL DEFAULT '', PRIMARY KEY (account_id, product_id))",
    ],
    [   # v20 -- why a rollout is paused: 'operator' (PATCH state=paused), 'superseded' (a
        # newer rollout took its (product, cohort)), or 'failure_limit' (auto-pause). NULL
        # while active/stopped. A dashboard's "needs attention" is the failure_limit ones.
        "ALTER TABLE rollouts ADD COLUMN pause_reason TEXT",
    ],
    [   # v21 -- product_id widens to 64 bits. It was a crc32, and 32 bits collide at a
        # few thousand products (birthday bound) -- fatal for a platform minting one per
        # end customer, because the id IS the device's cross-flash guard: a collision
        # offers one product line's firmware to another's devices. sqlite's INTEGER is
        # already 64-bit and it cannot ALTER a column type, so these run on Postgres only
        # (see _widen_statements).
        "-- postgres: ALTER TABLE releases ALTER COLUMN product_id TYPE BIGINT",
        "-- postgres: ALTER TABLE rollouts ALTER COLUMN product_id TYPE BIGINT",
        "-- postgres: ALTER TABLE devices ALTER COLUMN product_id TYPE BIGINT",
        "-- postgres: ALTER TABLE deployments ALTER COLUMN product_id TYPE BIGINT",
        "-- postgres: ALTER TABLE cohort_pins ALTER COLUMN product_id TYPE BIGINT",
        "-- postgres: ALTER TABLE products ALTER COLUMN product_id TYPE BIGINT",
    ],
    [   # v22 -- a token may be limited to some of its account's products. Empty/NULL is
        # the ordinary case: the whole account. Stored as a comma-separated id list beside
        # the scopes, which say what a token may DO; this says what it may do it TO.
        "ALTER TABLE admin_tokens ADD COLUMN products TEXT",
    ],
    [   # v23 -- device ids become board-qualified. The reported unit id is unique among
        # boards of one TYPE (it is the MCU die id) and not across types, so two cameras
        # could share a row: the sticky account binding would hand the second one the
        # first's account, and its live view and telemetry would open onto the first's.
        # Existing rows carry their board, so they are rekeyed in place.
        #
        # ORDER MATTERS. The tables that REFER to a device are rewritten first, while
        # devices still holds the old key to join on; rekeying devices first would strand
        # every binding and every install record against an id that no longer exists.
        "UPDATE device_accounts SET device_id = ("
        "  SELECT d.board || ':' || d.device_id FROM devices d "
        "  WHERE d.device_id = device_accounts.device_id) "
        "WHERE EXISTS (SELECT 1 FROM devices d WHERE d.device_id = device_accounts.device_id "
        "              AND d.board IS NOT NULL AND d.board != '')",
        "UPDATE deployments SET device_id = ("
        "  SELECT d.board || ':' || d.device_id FROM devices d "
        "  WHERE d.device_id = deployments.device_id) "
        "WHERE EXISTS (SELECT 1 FROM devices d WHERE d.device_id = deployments.device_id "
        "              AND d.board IS NOT NULL AND d.board != '')",
        "UPDATE devices SET device_id = board || ':' || device_id "
        "WHERE board IS NOT NULL AND board != '' AND device_id NOT LIKE '%:%'",
    ],
    [   # v24 -- an account remembers which operator credential created it, and the
        # caller's own reference for it. A server can carry more than one operator: our
        # own website, and a platform that resells the service to ITS customers. Without
        # this, `GET /accounts` hands every operator the whole tenant directory, and
        # account names have to be unique across all of them -- so two platforms cannot
        # both have a customer called "Acme", and the 409 that says so is a way to
        # enumerate the other platform's customers.
        #
        # `client_ref` is the creator's own id for the account -- a workspace or tenant
        # id in the platform provisioning it. It makes creation idempotent: a retry
        # after a timeout returns the account that already exists instead of a second
        # one, or an ambiguous 409.
        "ALTER TABLE accounts ADD COLUMN created_by TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN client_ref TEXT NOT NULL DEFAULT ''",
        "CREATE UNIQUE INDEX IF NOT EXISTS accounts_client_ref "
        "ON accounts (created_by, client_ref) WHERE client_ref != ''",
        # Existing accounts already know who made them: `account.create` records the
        # calling credential's name as its actor. Without this backfill every account on
        # a running server would be owned by nobody, and the website that created them
        # would get a 404 the next time it set a device limit -- a filter added for a
        # partner who is not onboarded yet, breaking the operator who is.
        "UPDATE accounts SET created_by = COALESCE(("
        "  SELECT a.actor FROM audit a WHERE a.action = 'account.create' "
        "  AND a.entity_id = accounts.account_id ORDER BY a.seq LIMIT 1), '')",
        # ...and an existing `accounts` token keeps the authority it already had. It
        # could manage every account on this server a moment ago; a migration is not the
        # place to take that away silently. New tokens get the narrower scope, which is
        # what a partner is issued.
        "UPDATE admin_tokens SET scopes = scopes || ',accounts.all' "
        "WHERE scopes LIKE '%accounts%' AND scopes NOT LIKE '%accounts.all%'",
    ],
    [   # v25 -- an audit row remembers the product it happened to, where there is one.
        # A product-limited token could not read the log AT ALL (an audit row records an
        # action, not a product, so there was nothing to filter it by) -- which is the
        # wrong answer for a platform that hands each of its customers a credential for
        # one product: they got a fleet they could drive and no history of it.
        #
        # It is deliberately NOT part of the hash chain. The chain covers what the entry
        # ASSERTS -- who did what, to which entity, when. This column is an index onto
        # the same act, added so a read can be filtered; folding it into the hash would
        # invalidate every entry already written.
        "ALTER TABLE audit ADD COLUMN product_id BIGINT",
    ],
    [   # v26 -- the account's publish counter, and the counter each release carries.
        # A camera built with product_id 0 can be moved between product lines, so its
        # images cannot be ordered by a per-product version; they are ordered by this,
        # which spans the account. The server allocates it because a counter is the one
        # piece of a build that cannot be replicated the way a signing key can -- two
        # concurrent builds must not take the same number.
        "ALTER TABLE accounts ADD COLUMN publish_seq BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE releases ADD COLUMN publish_seq BIGINT NOT NULL DEFAULT 0",
    ],
    [   # v27 -- a device pin becomes an INTENT rather than a field on the fleet row.
        # It was a column on `devices`, set by an UPDATE that matched nothing when the
        # camera had never checked in -- so pinning one before it was first powered on
        # silently did nothing, and a claim had to wait for a check-in to create the row
        # and then land on the one after that, with a customer watching. The account
        # binding has always worked this way (see device_accounts) for exactly this
        # reason; the pin was the odd one out.
        "CREATE TABLE IF NOT EXISTS device_pins ("
        "device_id TEXT PRIMARY KEY, release_id TEXT NOT NULL, "
        "account_id TEXT NOT NULL DEFAULT '', pinned_at TEXT)",
        "INSERT INTO device_pins (device_id, release_id, account_id) "
        "SELECT device_id, pinned_release_id, COALESCE(account_id, '') FROM devices "
        "WHERE pinned_release_id IS NOT NULL AND pinned_release_id != ''",
        # No longer written -- device_pins is the authority, and a reader that still
        # selects the column gets NULL rather than a value that has quietly stopped moving.
        "UPDATE devices SET pinned_release_id = NULL",
    ],
    [   # v28 -- the fleet row records the counter a camera is on, and whether it orders
        # by it. The offer decision already reads both off the check-in; without them on
        # the row an operator could not see, from `device show`, the one number that
        # decides whether a platform's camera will take what it is offered.
        "ALTER TABLE devices ADD COLUMN publish_seq BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE devices ADD COLUMN orders_by_seq INTEGER NOT NULL DEFAULT 0",
    ],
    [   # v29 -- webhooks. The audit log was always the event stream; this is its push
        # side: an account's endpoints, and one delivery row per (endpoint, audit entry)
        # that matched, carrying the retry state. Deliveries reference the audit seq
        # rather than copying the entry, so what is sent is exactly what the log says.
        "CREATE TABLE IF NOT EXISTS webhooks ("
        "webhook_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, url TEXT NOT NULL, "
        "secret TEXT NOT NULL, events TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, "
        "description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
        "created_by TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0, "
        "disabled_reason TEXT NOT NULL DEFAULT '', last_delivery_at TEXT, last_status INTEGER)",
        "CREATE INDEX IF NOT EXISTS idx_webhooks_account ON webhooks (account_id)",
        "CREATE TABLE IF NOT EXISTS webhook_deliveries ("
        "delivery_id TEXT PRIMARY KEY, webhook_id TEXT NOT NULL, audit_seq INTEGER NOT NULL, "
        "event TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempt INTEGER NOT NULL DEFAULT 0, "
        "next_at DOUBLE PRECISION NOT NULL, claimed_until DOUBLE PRECISION NOT NULL DEFAULT 0, "
        "last_code INTEGER, last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
        "delivered_at TEXT)",
        "CREATE INDEX IF NOT EXISTS idx_deliveries_due ON webhook_deliveries (status, next_at)",
        "CREATE INDEX IF NOT EXISTS idx_deliveries_hook ON webhook_deliveries (webhook_id, audit_seq)",
    ],
    [   # v30 -- account-leading read indexes. Every collection read (_scope leads its WHERE with
        # account_id) and the check-in's active_rollout lookup filter by account_id FIRST, but the
        # original indexes (idx_devices_board / idx_releases_board / idx_rollouts_board_cohort) lead
        # with product_id -- so at scale a query walks every account that shares a product_id before
        # the account_id filter applies. These lead with account_id to match how the rows are read.
        # ADDITIVE: the product_id-leading indexes stay, for the device-path lookups that carry no
        # account (an unregistered board is served read-only with account_id = ''). Index-only,
        # no data change, so it applies online. active_rollout (hottest, one per check-in) is the
        # one this most helps: (account_id, product_id, cohort, state) makes it a direct hit.
        "CREATE INDEX IF NOT EXISTS idx_rollouts_account "
        "ON rollouts (account_id, product_id, cohort, state)",
        "CREATE INDEX IF NOT EXISTS idx_devices_account ON devices (account_id, product_id, cohort)",
        "CREATE INDEX IF NOT EXISTS idx_releases_account "
        "ON releases (account_id, product_id, payload_version)",
    ],
    [   # v31 -- rollout ramps. A rollout may carry a declared ordered list of STAGES (JSON:
        # {percent, min_soak, min_attempted, max_failure_rate?}) it raises itself through, lazily,
        # on check-ins -- so an operator declares "1% for a day, then 10%, then 100%" once instead
        # of babysitting PATCH calls. NULL stages = a manual rollout, unchanged. Each stage is judged
        # on its OWN window, so the baselines record the attempted/failures totals when the current
        # stage began; stage_entered_at is when, for the soak clock.
        "ALTER TABLE rollouts ADD COLUMN stages TEXT",
        "ALTER TABLE rollouts ADD COLUMN stage_index INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE rollouts ADD COLUMN stage_entered_at TEXT",
        "ALTER TABLE rollouts ADD COLUMN stage_attempted_base INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE rollouts ADD COLUMN stage_failures_base INTEGER NOT NULL DEFAULT 0",
    ],
]


class SqlMetadataStore:
    """SQL metadata store over an open DBAPI ``connection``. ``paramstyle`` is ``?`` (sqlite) or
    ``%s`` (postgres); SQL is authored with ``?`` and translated on the way out."""

    paramstyle = "?"

    def __init__(self, connection):
        self._conn = connection
        self._lock = threading.Lock()

    def _sql(self, sql: str) -> str:
        return sql if self.paramstyle == "?" else sql.replace("?", "%s")

    def _run(self, cur, sql: str, params: tuple):
        """Execute, passing NO parameter sequence when there are none.

        psycopg treats an empty sequence as "interpolate this", so a literal ``%`` in
        the SQL -- ``LIKE '%:%'`` in a migration, say -- raises before the statement
        ever reaches the server. sqlite3 ignores the distinction, so the failure exists
        only in production, which is exactly where a migration must not fail."""
        if params:
            cur.execute(self._sql(sql), params)
        else:
            cur.execute(self._sql(sql))
        return cur

    def execute(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.cursor()
            self._run(cur, sql, params)
            self._conn.commit()
            return cur

    def query_one(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.cursor()
            self._run(cur, sql, params)
            row = cur.fetchone()
            self._conn.commit()          # a read ends its transaction: no lock outlives it
            return row

    def query_all(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            cur = self._conn.cursor()
            self._run(cur, sql, params)
            rows = list(cur.fetchall())
            self._conn.commit()          # (a DBAPI connection opens one on the first statement)
            return rows

    _LOCK_RETRIES = 10
    _LOCK_BACKOFF_S = 6

    def _migrate_stmt(self, stmt: str) -> None:
        """Run one migration statement, retrying while the lock is held elsewhere.

        A type change takes an ACCESS EXCLUSIVE lock, and a zero-downtime deploy runs
        this while the PREVIOUS instance is still serving: its ordinary short reads are
        enough to keep the lock away, and the bounded ``lock_timeout`` then fails the
        statement rather than queue behind them forever. Failing means the container
        exits, the platform keeps the old instance, and the deploy silently never
        happens -- the failure mode this whole migration path already cost us once.

        So a lock refusal is retried, not fatal: each attempt is still bounded, and a
        minute of retries crosses the gap between one instance's requests. Anything that
        is not a lock problem raises immediately, because a broken migration must be
        loud."""
        for attempt in range(1, self._LOCK_RETRIES + 1):
            try:
                self.execute(self._dialect(stmt))
                return
            except Exception as e:                                   # noqa: BLE001
                # A failed statement ABORTS the transaction in Postgres: every later
                # command raises InFailedSqlTransaction until a rollback. So tolerating
                # or retrying is only half the job -- without this the next statement
                # fails for a different reason, which is exactly how the first version
                # of this tolerance turned a DuplicateColumn into an aborted-transaction
                # crash one line later.
                self._rollback()
                if self._is_already_applied(e):
                    # The object this statement creates is already there, so the
                    # statement's work is done. That state is reachable: each statement
                    # commits as it runs, but schema_version is only written after the
                    # WHOLE pending list succeeds -- so a migration that fails partway
                    # leaves real changes behind a version number that denies them, and
                    # every deploy after it re-runs an ADD COLUMN that cannot succeed.
                    # Production sat on an old build for a day over exactly this.
                    print("migrate: already applied, skipping -- %s" % stmt[:70],
                          file=sys.stderr, flush=True)
                    return
                if not self._is_lock_error(e) or attempt == self._LOCK_RETRIES:
                    raise
                print("migrate: lock busy (attempt %d/%d), retrying in %ds -- %s"
                      % (attempt, self._LOCK_RETRIES, self._LOCK_BACKOFF_S, stmt[:60]),
                      file=sys.stderr, flush=True)
                time.sleep(self._LOCK_BACKOFF_S)

    def _rollback(self) -> None:
        """Make the connection usable again after a failed statement."""
        try:
            self._conn.rollback()
        except Exception:                      # pragma: no cover - driver without rollback
            pass

    # Postgres SQLSTATEs for "the thing you are creating already exists": duplicate
    # column, table, object, index. sqlite says it in words instead.
    _ALREADY = ("42701", "42P07", "42710", "42P16")

    @classmethod
    def _is_already_applied(cls, exc: Exception) -> bool:
        """Whether a failed migration statement failed because its work is already done.

        Narrow on purpose: only "already exists" on a create/add. A migration that is
        wrong in any other way must still be loud, because a silently half-applied
        schema is worse than a failed deploy."""
        state = getattr(exc, "sqlstate", None) or getattr(
            getattr(exc, "diag", None), "sqlstate", None)
        if state in cls._ALREADY:
            return True
        text = str(exc).lower()
        return ("already exists" in text or "duplicate column" in text)

    @staticmethod
    def _is_lock_error(exc: Exception) -> bool:
        """Whether an exception is "someone else holds the lock", not "this SQL is wrong".
        Matched on SQLSTATE 55P03 (lock_not_available) with a message fallback, so it
        works whichever driver raised it."""
        if getattr(exc, "sqlstate", None) == "55P03" or getattr(
                getattr(exc, "diag", None), "sqlstate", None) == "55P03":
            return True
        text = str(exc).lower()
        return "lock timeout" in text or "lock_not_available" in text

    def migrate(self) -> int:
        """Create the ``meta`` table and apply any migrations past the recorded ``schema_version``.
        Returns the resulting schema version. Idempotent."""
        self.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        current = int(self.get_meta("schema_version") or 0)
        pending = [(v, s) for v, s in enumerate(_MIGRATIONS, start=1) if v > current]
        if pending:
            self._before_migrations()
            for version, statements in pending:
                for stmt in statements:
                    self._migrate_stmt(stmt)
                current = version
                # Record progress per VERSION, not once at the end. Each statement
                # commits as it runs, so a failure partway through the list used to
                # leave real schema changes behind a version number that still denied
                # them -- and the next deploy would replay an ADD COLUMN that could only
                # fail. Recording here means a half-finished migration resumes from
                # where it stopped instead of from the beginning.
                self.set_meta("schema_version", str(current))
            self._after_migrations()
        self.set_meta("schema_version", str(current))
        return current

    def _dialect(self, stmt: str) -> str:
        """A migration step written for one backend only.

        ``-- postgres: <sql>`` runs as ``<sql>`` on Postgres and stays a comment (a
        no-op) on sqlite, which is how a column-type widening ships: sqlite's INTEGER is
        already 64-bit and sqlite has no ALTER COLUMN TYPE at all."""
        return stmt

    def _before_migrations(self) -> None:
        """Backend hook run once before pending migrations apply (Postgres: lock hygiene)."""

    def _after_migrations(self) -> None:
        """Backend hook run once after pending migrations applied."""

    def get_meta(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    # --- releases ---------------------------------------------------------------------------

    def add_release(self, *, release_id, product_id, product, version, payload_version,
                    min_platform_version, image_sha256, image_size, representations,
                    manifest_key, image_key, delta_key=None, key_id=None, uploaded_by=None,
                    account_id="", dev=0, sbom_key=None, publish_seq=0,
                    display_name="") -> None:
        self.execute(
            "INSERT INTO releases (release_id, product_id, product, version, payload_version, "
            "publish_seq, "
            "min_platform_version, image_sha256, image_size, representations, manifest_key, "
            "image_key, delta_key, key_id, uploaded_by, uploaded_at, account_id, dev, sbom_key, "
            "display_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (release_id, product_id, product, version, payload_version, publish_seq,
             min_platform_version,
             image_sha256, image_size, json.dumps(representations), manifest_key, image_key,
             delta_key, key_id, uploaded_by, _now_iso(), account_id, dev, sbom_key,
             display_name))

    def get_release(self, release_id: str) -> dict | None:
        r = _d(self.query_one("SELECT * FROM releases WHERE release_id = ?", (release_id,)))
        if r is not None:
            r["representations"] = json.loads(r["representations"])
        return r

    RELEASE_SORTS = {"version": "payload_version", "product": "product", "size": "image_size",
                     "uploaded": "uploaded_at", "name": "display_name COLLATE NOCASE", "release": "release_id"}

    def count_releases(self, product_id=None, account_id=None, products=None) -> int:
        where, params = _scope(account_id, product_id, products)
        return self.query_one("SELECT COUNT(*) AS n FROM releases " + where, params)["n"]

    def list_releases(self, product_id=None, account_id=None, limit=None, offset=0,
                      sort=None, direction=None, products=None) -> list[dict]:
        where, params = _scope(account_id, product_id, products)
        sql = ("SELECT * FROM releases " + where
               + _order(sort, direction, self.RELEASE_SORTS, "payload_version DESC", "release_id"))
        sql, params = _limit(sql, params, limit, offset)
        rows = [_d(r) for r in self.query_all(sql, params)]
        for r in rows:
            r["representations"] = json.loads(r["representations"])
        return rows

    def latest_release_payload_version(self, product_id: int, account_id=None) -> int | None:
        where, params = _scope(account_id, product_id)
        return self.query_one(
            "SELECT MAX(payload_version) AS m FROM releases " + where, params)["m"]

    # --- rollouts ---------------------------------------------------------------------------

    def add_rollout(self, *, rollout_id, release_id, product_id, cohort, percent, state="active",
                    failure_threshold=0.05, account_id="", display_name="", stages=None) -> None:
        now = _now_iso()
        # ``stages`` (a validated list) is stored as JSON; a ramp starts at stage 0, entered now,
        # with zero baselines (the rollout's own attempted/failures also start at 0). NULL = no ramp.
        stages_json = json.dumps(stages) if stages else None
        entered = now if stages else None
        self.execute(
            "INSERT INTO rollouts (rollout_id, release_id, product_id, cohort, percent, state, "
            "failure_threshold, created_at, updated_at, account_id, display_name, stages, "
            "stage_entered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rollout_id, release_id, product_id, cohort, percent, state, failure_threshold, now,
             now, account_id, display_name, stages_json, entered))

    def get_rollout(self, rollout_id: str) -> dict | None:
        return _d(self.query_one("SELECT * FROM rollouts WHERE rollout_id = ?", (rollout_id,)))

    def active_rollout(self, product_id: int, cohort: str, account_id: str = "") -> dict | None:
        return _d(self.query_one(
            "SELECT * FROM rollouts WHERE account_id = ? AND product_id = ? AND cohort = ? "
            "AND state = 'active' ORDER BY created_at DESC LIMIT 1", (account_id, product_id, cohort)))

    ROLLOUT_SORTS = {"created": "r.created_at", "percent": "r.percent", "state": "r.state",
                     "cohort": "r.cohort", "product": "r.product_id", "name": "r.display_name COLLATE NOCASE",
                     "devices": "cohort_devices", "rollout": "r.rollout_id"}

    @staticmethod
    def _rollouts_where(account_id, product_id, state, cohort, release_id=None,
                        pause_reason=None, products=None) -> tuple[str, tuple]:
        where, params = _scope(account_id, product_id, products)
        where = where.replace("account_id", "r.account_id").replace("product_id", "r.product_id")
        if state is not None:
            where, params = _and(where, "r.state = ?"), (*params, state)
        if pause_reason is not None:                 # "paused by the failure limit" etc.
            where, params = _and(where, "r.pause_reason = ?"), (*params, pause_reason)
        if cohort is not None:                       # "what targets this cohort"
            where, params = _and(where, "r.cohort = ?"), (*params, cohort)
        if release_id is not None:                   # "what ships this release"
            where, params = _and(where, "r.release_id = ?"), (*params, release_id)
        return where, params

    def count_rollouts(self, product_id=None, account_id=None, state=None, cohort=None,
                       release_id=None, pause_reason=None, products=None) -> int:
        where, params = self._rollouts_where(account_id, product_id, state, cohort, release_id,
                                             pause_reason, products)
        return self.query_one("SELECT COUNT(*) AS n FROM rollouts r " + where, params)["n"]

    def list_rollouts(self, product_id: int | None = None, account_id=None, limit=None,
                      offset=0, state: str | None = None, cohort: str | None = None,
                      sort=None, direction=None, release_id: str | None = None,
                      pause_reason: str | None = None, products=None) -> list[dict]:
        # cohort_devices: how many devices sit in each rollout's (product, cohort) RIGHT NOW --
        # the audience its percent applies to. Computed live rather than stored, because cohort
        # membership shifts under the rollout (assignments, first check-ins).
        # up_to_date: how many of those devices run the rollout's release or something
        # newer -- the progress a list can show honestly (the offer percent is a dial, and
        # the counters count transitions, not devices).
        where, params = self._rollouts_where(account_id, product_id, state, cohort, release_id,
                                             pause_reason, products)
        sql = ("SELECT r.*, (SELECT COUNT(*) FROM devices d WHERE d.product_id = r.product_id "
               "AND d.cohort = r.cohort AND d.account_id = r.account_id) AS cohort_devices, "
               "(SELECT COUNT(*) FROM devices d JOIN releases rel ON rel.release_id = r.release_id "
               "WHERE d.product_id = r.product_id AND d.cohort = r.cohort "
               "AND d.account_id = r.account_id "
               "AND d.current_payload_version >= rel.payload_version) AS up_to_date "
               "FROM rollouts r " + where
               + _order(sort, direction, self.ROLLOUT_SORTS, "r.created_at DESC", "r.rollout_id"))
        sql, params = _limit(sql, params, limit, offset)
        return [_d(r) for r in self.query_all(sql, params)]

    def cohort_in_use(self, cohort: str, account_id: str = "") -> bool:
        """Whether any device, rollout, or pin in the account uses ``cohort``."""
        for table in ("devices", "rollouts", "cohort_pins", "cohorts"):
            if self.query_one("SELECT 1 AS x FROM %s WHERE account_id = ? AND cohort = ? "
                              "LIMIT 1" % table, (account_id, cohort)):
                return True
        return False

    def create_cohort(self, cohort: str, account_id: str = "") -> None:
        """Declare a label so it exists with no devices yet. Validation (non-empty, not
        __default__, not already in use) is the caller's."""
        self.execute("INSERT INTO cohorts (account_id, cohort, created_at) VALUES (?, ?, ?)",
                     (account_id, cohort, _now_iso()))

    def declare_cohort(self, cohort: str, account_id: str = "") -> None:
        """Idempotent declaration -- what `assign` does implicitly so a label that sprang
        into being on a device row is also on the books. __default__ is never declared."""
        if cohort == "__default__" or account_id is None:
            return
        if not self.query_one("SELECT 1 AS x FROM cohorts WHERE account_id = ? AND cohort = ?",
                              (account_id, cohort)):
            self.execute("INSERT INTO cohorts (account_id, cohort, created_at) VALUES (?, ?, ?)",
                         (account_id, cohort, _now_iso()))

    def rename_cohort(self, old: str, new: str, account_id: str = "") -> dict:
        """Relabel a cohort everywhere it is referenced -- device rows, rollouts, pins --
        in ONE commit, so a crash can't leave the name half-changed and a rollout keeps
        reaching exactly the devices it did (its staged set hashes on rollout_id +
        device_id, never the name). Returns the per-table counts. Validation (refusing
        __default__ and an in-use target) is the caller's."""
        counts = {}
        with self._lock:
            cur = self._conn.cursor()
            for key, table in (("devices", "devices"), ("rollouts", "rollouts"),
                               ("pins", "cohort_pins")):
                cur.execute(self._sql(
                    "UPDATE %s SET cohort = ? WHERE account_id = ? AND cohort = ?" % table),
                    (new, account_id, old))
                counts[key] = cur.rowcount
            # the declaration follows the label (the target was verified unused)
            cur.execute(self._sql("DELETE FROM cohorts WHERE account_id = ? AND cohort = ?"),
                        (account_id, old))
            cur.execute(self._sql("INSERT INTO cohorts (account_id, cohort, created_at) "
                                  "VALUES (?, ?, ?)"), (account_id, new, _now_iso()))
            self._conn.commit()
        return counts

    def cohort_has_active_rollout(self, cohort: str, account_id: str = "") -> bool:
        return bool(self.query_one(
            "SELECT 1 AS x FROM rollouts WHERE account_id = ? AND cohort = ? "
            "AND state = 'active' LIMIT 1", (account_id, cohort)))

    def delete_cohort(self, cohort: str, account_id: str = "") -> dict:
        """Retire a label: its devices return to __default__ and its pins drop, in one
        commit. Rollout rows keep the name -- they are history. Validation (refusing
        __default__ and a cohort an active rollout still targets) is the caller's."""
        counts = {}
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql("UPDATE devices SET cohort = '__default__' "
                                  "WHERE account_id = ? AND cohort = ?"), (account_id, cohort))
            counts["devices"] = cur.rowcount
            cur.execute(self._sql("DELETE FROM cohort_pins WHERE account_id = ? AND cohort = ?"),
                        (account_id, cohort))
            counts["pins"] = cur.rowcount
            cur.execute(self._sql("DELETE FROM cohorts WHERE account_id = ? AND cohort = ?"),
                        (account_id, cohort))
            self._conn.commit()
        return counts

    def cohort_device_count(self, product_id: int, cohort: str, account_id: str = "") -> int:
        """How many devices are in ``(product, cohort)`` right now -- a rollout's audience."""
        return self.query_one(
            "SELECT COUNT(*) AS n FROM devices WHERE account_id = ? AND product_id = ? "
            "AND cohort = ?", (account_id, product_id, cohort))["n"]

    def update_rollout(self, rollout_id: str, **fields) -> None:
        fields = {**fields, "updated_at": _now_iso()}       # column names are code-controlled
        assigns = ", ".join(k + " = ?" for k in fields)
        self.execute("UPDATE rollouts SET " + assigns + " WHERE rollout_id = ?",
                     (*fields.values(), rollout_id))

    def bump_rollout(self, rollout_id: str, *, attempted=0, updated=0, failures=0) -> None:
        self.execute(
            "UPDATE rollouts SET attempted = attempted + ?, updated = updated + ?, "
            "failures = failures + ?, updated_at = ? WHERE rollout_id = ?",
            (attempted, updated, failures, _now_iso(), rollout_id))

    # --- the device registry (registered devices only) --------------------------------------

    def upsert_device(self, *, device_id, product_id, board=None, cohort="__default__",
                      current_version=None, current_payload_version=None, slot=None,
                      representation=None, fallback_reason=None, confirmed=None,
                      last_offered_release_id=None, registrar_ref=None, account_id="",
                      streams=None, fallback_payload_version=None, body_sha256=None,
                      publish_seq=0, orders_by_seq=False) -> None:
        # One statement, not select-then-insert: the store's lock covers a statement, not a
        # handler, so two first check-ins of the same new device (a retry racing the request it
        # retried) both saw no row and the second INSERT died on the primary key -- a 500 to the
        # camera. ON CONFLICT makes the second one the UPDATE it was always meant to be.
        # cohort is admin-controlled, so a check-in never changes it. COALESCE on the update
        # side: a device that stops reporting slots (or never did) keeps whatever it last told
        # us, rather than having its fallback silently blanked.
        now = _now_iso()
        self.execute(
            "INSERT INTO devices (device_id, product_id, board, cohort, current_version, "
            "current_payload_version, slot, representation, fallback_reason, confirmed, "
            "last_offered_release_id, registrar_ref, account_id, streams, "
            "fallback_payload_version, body_sha256, publish_seq, orders_by_seq, "
            "first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (device_id) DO UPDATE SET "
            "product_id = excluded.product_id, board = excluded.board, "
            "current_version = excluded.current_version, "
            "current_payload_version = excluded.current_payload_version, "
            "slot = excluded.slot, representation = excluded.representation, "
            "fallback_reason = excluded.fallback_reason, confirmed = excluded.confirmed, "
            "last_offered_release_id = COALESCE(excluded.last_offered_release_id, "
            "devices.last_offered_release_id), "
            "registrar_ref = COALESCE(excluded.registrar_ref, devices.registrar_ref), "
            "account_id = excluded.account_id, "
            "streams = COALESCE(?, devices.streams), "
            "fallback_payload_version = COALESCE(excluded.fallback_payload_version, "
            "devices.fallback_payload_version), "
            "body_sha256 = COALESCE(excluded.body_sha256, devices.body_sha256), "
            "publish_seq = excluded.publish_seq, orders_by_seq = excluded.orders_by_seq, "
            "last_seen = excluded.last_seen",
            (device_id, product_id, board, cohort, current_version, current_payload_version,
             slot, representation, fallback_reason, confirmed, last_offered_release_id,
             registrar_ref, account_id, ",".join(streams or ()), fallback_payload_version,
             body_sha256, int(publish_seq or 0), 1 if orders_by_seq else 0, now, now,
             ",".join(streams) if streams else None))

    def fleet_bases(self, product_id=None, account_id="", products=None) -> list[dict]:
        """The distinct (payload_version, body_sha256) bases the fleet is RUNNING, with device
        counts -- the answer to "which delta bases must this release cover?". Grouped by exact
        bytes, not just version: two groups for one version means a republish split the fleet,
        and only the group matching the store's bytes can take a delta."""
        where, args = ["current_payload_version IS NOT NULL"], []
        if product_id is not None:
            where.append("product_id = ?")
            args.append(product_id)
        if account_id:
            where.append("account_id = ?")
            args.append(account_id)
        if products is not None:                 # a product-limited credential
            if products:
                where.append("product_id IN (%s)" % ",".join(["?"] * len(products)))
                args.extend(products)
            else:
                where.append("1 = 0")
        rows = self.query_all(
            "SELECT current_payload_version AS payload_version, "
            "COALESCE(body_sha256, '') AS body_sha256, COUNT(*) AS devices "
            "FROM devices WHERE " + " AND ".join(where) + " "
            "GROUP BY current_payload_version, COALESCE(body_sha256, '') "
            "ORDER BY devices DESC, payload_version DESC", tuple(args))
        return [_d(r) for r in rows]

    def get_device(self, device_id: str) -> dict | None:
        row = _d(self.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,)))
        if row is not None:
            row["pinned_release_id"] = self.get_device_pin(device_id)
        return row

    def forget_device(self, device_id: str) -> None:
        """Remove a device from the fleet: its row and its account binding.

        What is NOT removed is its install history. A deployment row says what happened
        on a day that has already passed, and rollout counters and `/fleet/installs` are
        built from those rows -- deleting them would quietly restate history to say the
        installs never happened. The device is gone; what it did is not.

        Nor is the audit touched: it is append-only and hash-chained, and the removal is
        itself an entry in it.

        A camera that checks in again after this is simply a device the server has not
        seen before -- it enrols from scratch, with a `learned` binding."""
        self.execute("DELETE FROM device_accounts WHERE device_id = ?", (device_id,))
        self.execute("DELETE FROM device_pins WHERE device_id = ?", (device_id,))
        self.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))

    # --- sticky device -> account binding (the authoritative account for the device path) ----

    def bind_device_account(self, device_id: str, account_id: str, *, source: str) -> None:
        """Bind a device to an account. ``source='learned'`` is **sticky** -- it only takes if the
        device is unbound, so a later (or downgraded '') report never changes it. ``source='admin'``
        is an operator override and always wins."""
        now = _now_iso()
        if source == "admin":
            self.execute(
                "INSERT INTO device_accounts (device_id, account_id, source, bound_at) VALUES (?,?,?,?) "
                "ON CONFLICT (device_id) DO UPDATE SET account_id = excluded.account_id, "
                "source = excluded.source, bound_at = excluded.bound_at",
                (device_id, account_id, source, now))
        else:
            self.execute(
                "INSERT INTO device_accounts (device_id, account_id, source, bound_at) VALUES (?,?,?,?) "
                "ON CONFLICT (device_id) DO NOTHING", (device_id, account_id, source, now))

    def device_account(self, device_id: str) -> dict | None:
        """The device's binding row ``{account_id, source}`` or None (unbound)."""
        return _d(self.query_one(
            "SELECT account_id, source FROM device_accounts WHERE device_id = ?", (device_id,)))

    def set_device_account(self, device_id: str, account_id: str) -> None:
        """Set the ``devices`` row's account (no-op if the row doesn't exist yet). Used by an admin
        rebind so the fleet views reflect the new account immediately, not on the next check-in."""
        self.execute("UPDATE devices SET account_id = ? WHERE device_id = ?", (account_id, device_id))

    DEVICE_SORTS = {"seen": "last_seen", "device": "COALESCE(NULLIF(display_name, ''), device_id) COLLATE NOCASE",
                    "product": "product_id", "version": "current_version", "cohort": "cohort",
                    "first_seen": "first_seen"}

    @staticmethod
    def _devices_where(account_id, product_id, cohort, q, cohort_not,
                       version=None, older_than_pv=None, fell_back=None, unconfirmed=None,
                       not_seen_since=None, products=None, seen_since=None,
                       behind=None, up_to_date=None, installed_on=None,
                       failed_on=None) -> tuple[str, tuple]:
        where, params = _scope(account_id, product_id, products)
        if cohort is not None:
            where, params = _and(where, "cohort = ?"), (*params, cohort)
        if fell_back:                                # last boot rejected a slot
            where = _and(where, "fallback_reason IS NOT NULL")
        if unconfirmed:                              # mid-trial: deferring further updates
            where = _and(where, "confirmed = 0")
        if not_seen_since is not None:               # quiet: no check-in since this instant
            where = _and(where, "(last_seen IS NULL OR last_seen < ?)")
            params = (*params, _iso_at(not_seen_since))
        if seen_since is not None:                   # the exact complement: alive since then
            where = _and(where, "last_seen >= ?")
            params = (*params, _iso_at(seen_since))
        # behind / up_to_date: measured against the device's OWN product's newest release,
        # which `older_than_release` cannot express (it takes one release id, and a fleet
        # spans products with separate version histories). "Behind" is "a newer release
        # exists for my product"; "up to date" is its complement AMONG devices whose
        # product has published something -- a product with no release leaves its devices
        # in neither set, exactly as the fleet summary's `measured` counts them.
        _NEWER = ("EXISTS (SELECT 1 FROM releases r WHERE r.product_id = devices.product_id "
                  "AND r.account_id = devices.account_id "
                  "AND r.payload_version > COALESCE(devices.current_payload_version, -1))")
        _ANY_REL = ("EXISTS (SELECT 1 FROM releases r WHERE r.product_id = devices.product_id "
                    "AND r.account_id = devices.account_id)")
        if behind:
            where = _and(where, _NEWER)
        if up_to_date:
            where = _and(where, _ANY_REL + " AND NOT " + _NEWER)
        # the install series' columns as lists: the devices whose deployment for some
        # release was last reported installed (or failed) on that UTC day
        for day, status in ((installed_on, "installed"), (failed_on, "failed")):
            if day is not None:
                where = _and(where, "EXISTS (SELECT 1 FROM deployments dp "
                                    "WHERE dp.device_id = devices.device_id "
                                    "AND dp.account_id = devices.account_id "
                                    "AND dp.status = ? AND substr(dp.reported_at, 1, 10) = ?)")
                params = (*params, status, day)
        if version is not None:                      # "running exactly this version"
            where, params = _and(where, "current_version = ?"), (*params, version)
        if older_than_pv is not None:                # "not yet on (or past) this release"
            where = _and(where, "(current_payload_version IS NULL OR current_payload_version < ?)")
            params = (*params, older_than_pv)
        if cohort_not is not None:                   # a picker: everything NOT yet in it
            where, params = _and(where, "cohort != ?"), (*params, cohort_not)
        if q:                                        # name-or-id substring, case-insensitive
            like = "%" + q.lower() + "%"
            where = _and(where, "(LOWER(device_id) LIKE ? OR LOWER(display_name) LIKE ?)")
            params = (*params, like, like)
        return where, params

    def count_devices(self, product_id=None, account_id=None, cohort=None, q=None,
                      cohort_not=None, version=None, older_than_pv=None, fell_back=None,
                      unconfirmed=None, not_seen_since=None, products=None,
                      seen_since=None, behind=None, up_to_date=None,
                      installed_on=None, failed_on=None) -> int:
        where, params = self._devices_where(account_id, product_id, cohort, q, cohort_not,
                                            version, older_than_pv, fell_back, unconfirmed,
                                            not_seen_since, products, seen_since,
                                            behind, up_to_date, installed_on, failed_on)
        return self.query_one("SELECT COUNT(*) AS n FROM devices " + where, params)["n"]

    def list_devices(self, product_id: int | None = None, limit: int = 100, account_id=None,
                     cohort=None, offset: int = 0, sort=None, direction=None, q=None,
                     cohort_not=None, version=None, older_than_pv=None,
                     fell_back=None, unconfirmed=None, not_seen_since=None,
                     products=None, seen_since=None, behind=None,
                     up_to_date=None, installed_on=None, failed_on=None) -> list[dict]:
        where, params = self._devices_where(account_id, product_id, cohort, q, cohort_not,
                                            version, older_than_pv, fell_back, unconfirmed,
                                            not_seen_since, products, seen_since,
                                            behind, up_to_date, installed_on, failed_on)
        rows = self.query_all("SELECT * FROM devices " + where
                              + _order(sort, direction, self.DEVICE_SORTS, "last_seen DESC", "device_id")
                              + " LIMIT ? OFFSET ?", (*params, limit, offset))
        out = [_d(r) for r in rows]
        pins = self.device_pins_for(r["device_id"] for r in out)
        for r in out:
            r["pinned_release_id"] = pins.get(r["device_id"])
        return out

    def list_products(self, account_id=None, products=None) -> list[dict]:
        """The account's products: every product id seen on a device or a release, with
        the friendly name from its newest release (None until one is published) and
        device / release counts. The dashboard's product directory."""
        where, params = _scope(account_id, products=products)
        devs = {r["product_id"]: r["n"] for r in self.query_all(
            "SELECT product_id, COUNT(*) AS n FROM devices " + where + " GROUP BY product_id",
            params)}
        rels = {r["product_id"]: r["n"] for r in self.query_all(
            "SELECT product_id, COUNT(*) AS n FROM releases " + where + " GROUP BY product_id",
            params)}
        newest = {}                       # product_id -> its newest release's (name, version, pv)
        for r in self.query_all("SELECT product_id, product, version, payload_version FROM releases "
                                + where + " ORDER BY payload_version DESC", params):
            newest.setdefault(r["product_id"], _d(r))
        labels = self.product_names(account_id)
        # devices at or past the newest release: adoption in one figure per product
        by_pv: dict = {}
        for r in self.query_all("SELECT product_id, current_payload_version AS pv, COUNT(*) AS n "
                                "FROM devices " + where + " GROUP BY product_id, pv", params):
            by_pv.setdefault(r["product_id"], []).append((r["pv"], r["n"]))
        rows = []
        # The DECLARED products (POST /products) join the ones seen on a device or a
        # release, so a project appears in the directory the moment it is created --
        # before it has either, which is when a platform wants to name it and bind its
        # first cameras. Declared ids go through the same allow-list as the rest.
        for pid in {*devs, *rels, *self.declared_products(account_id, products=products)}:
            manifest = (newest.get(pid) or {}).get("product")
            npv = (newest.get(pid) or {}).get("payload_version")
            up = (sum(n for pv, n in by_pv.get(pid, []) if pv is not None and pv >= npv)
                  if npv is not None else 0)
            # built by hand rather than from a row, so the JS-safe string is explicit
            rows.append({"product_id": pid, "product_id_str": str(pid),
                         "product": labels.get(pid) or manifest,
                         "display_name": labels.get(pid, ""), "manifest_name": manifest,
                         "devices": devs.get(pid, 0), "releases": rels.get(pid, 0),
                         "newest_version": (newest.get(pid) or {}).get("version"),
                         "newest_payload_version": npv, "up_to_date": up})
        rows.sort(key=lambda p: ((p["product"] or "").lower(), p["product_id"]))
        return rows

    def product_manifest_name(self, product_id: int, account_id=None) -> str | None:
        """The manifest product name already recorded against this product id, or None.

        A product id is **64 bits of sha256("<product>:<board>")**, so two names landing
        on one id is remote rather than likely (it was a 32-bit crc32, where a few
        thousand products made it a coin flip). Publishing under a colliding id would
        silently merge two product lines: one line's devices would be offered the
        other's firmware. This is what publish compares against to refuse that."""
        where, params = _scope(account_id, product_id)
        row = self.query_one("SELECT product FROM releases " + where
                             + " AND product IS NOT NULL AND product != '' "
                             "ORDER BY payload_version DESC LIMIT 1", params)
        return row["product"] if row else None

    def product_names(self, account_id=None) -> dict:
        """product_id -> the operator's display name (only products with one set)."""
        where, params = _scope(account_id)
        return {r["product_id"]: r["display_name"] for r in self.query_all(
            "SELECT product_id, display_name FROM products " + where, params)
            if r["display_name"]}

    def declared_products(self, account_id=None, products=None) -> set:
        """The product ids this account has DECLARED, named or not.

        ``product_names`` cannot answer this: it drops rows with no display name, and a
        product declared before it has a release often has no name yet -- declaring it is
        how a platform reserves the id and starts binding cameras to it."""
        where, params = _scope(account_id, products=products)
        return {r["product_id"] for r in
                self.query_all("SELECT product_id FROM products " + where, params)}

    def set_product_name(self, product_id: int, name: str, account_id: str = "") -> None:
        """Set a product's display name (empty = clear). A label only: the product id
        stays the identity, and the manifest name shows again when cleared."""
        self.execute(
            "INSERT INTO products (account_id, product_id, display_name) VALUES (?, ?, ?) "
            "ON CONFLICT (account_id, product_id) DO UPDATE SET display_name = excluded.display_name",
            (account_id, product_id, name))

    PRODUCT_SORTS = {"product": lambda p: ((p["product"] or "").lower(), p["product_id"]),
                     "devices": lambda p: p["devices"], "releases": lambda p: p["releases"],
                     "newest": lambda p: p["newest_payload_version"] or -1,
                     "up_to_date": lambda p: p["up_to_date"],
                     "share": lambda p: (p["up_to_date"] / p["devices"]) if p["devices"] else -1.0}

    def page_products(self, account_id=None, sort=None, direction=None, limit=None,
                      products=None,
                      offset=0) -> tuple[list[dict], int]:
        """``list_products`` on the list contract: (page, total). Small and aggregated,
        so sorted here with the same whitelist idea as the SQL lists."""
        rows = self.list_products(account_id=account_id, products=products)
        key = self.PRODUCT_SORTS.get(sort or "product", self.PRODUCT_SORTS["product"])
        rows.sort(key=key, reverse=(str(direction).lower() == "desc"))
        total = len(rows)
        rows = rows[offset: offset + limit] if limit is not None else rows[offset:]
        return rows, total

    def fleet_summary(self, product_id: int | None = None, account_id=None, products=None,
                      cohort: str | None = None, totals: bool = False) -> dict:
        """The fleet, structured PER PRODUCT -- a dashboard's shape, not a flat rollup.

        Version strings, fallbacks, and cohort compositions only mean anything within
        one product (an account's five products have five version histories), so every
        breakdown nests under ``products``; the top level keeps only the account-wide
        alarms. Fields per product:

          by_version   -- what that product's devices are running
          by_fallback  -- what they would fall back TO (packed versions; the API layer
                          decodes). A fleet with the previous release behind it is in a
                          very different position from one reporting nothing.
          by_cohort    -- how the product's devices are grouped
          fell_back    -- devices whose last boot REJECTED a slot. The direct alarm.
          unconfirmed  -- devices mid-trial (also the devices deferring updates).
          up_to_date   -- devices at or past the product's newest release. Summed at the
                          top level too, so a dashboard reads fleet adoption without
                          paging every product and adding it up itself.
          measured     -- the devices that COUNT toward that: a product with nothing
                          published yet has no newest release to be behind of, so its
                          devices are outside the ratio rather than 0% of it. Summed at
                          the top level too; that sum is adoption's denominator.

        ``totals`` returns the account-wide counters ALONE (``products`` empty). An
        overview reads four numbers; an account with thousands of products would ship it
        a per-product breakdown, with every version and cohort in it, to get them."""
        where, params = _scope(account_id, product_id, products)
        if cohort is not None:                       # scope to one rollout's audience
            where = (where + " AND cohort = ?") if where else "WHERE cohort = ?"
            params = (*params, cohort)

        def _grouped(col):
            out: dict[int, dict] = {}
            for r in self.query_all(
                    "SELECT product_id, %s AS k, COUNT(*) AS n FROM devices " % col
                    + where + " GROUP BY product_id, %s" % col, params):
                out.setdefault(r["product_id"], {})[r["k"]] = r["n"]
            return out

        by_version = {} if totals else _grouped("current_version")
        by_fallback = {} if totals else _grouped("fallback_payload_version")
        by_cohort = {} if totals else _grouped("cohort")
        # version string -> the release behind it (newest when a version was republished),
        # so a dashboard can link a running version to its release without a second read
        releases: dict[int, dict] = {}
        newest_pv: dict[int, int] = {}           # product_id -> its newest release's payload version
        rel_where, rel_params = _scope(account_id, product_id)      # releases have no cohort
        if totals:                               # adoption needs the newest payload version alone
            for r in self.query_all("SELECT product_id, MAX(payload_version) AS pv FROM releases "
                                    + rel_where + " GROUP BY product_id", rel_params):
                newest_pv[r["product_id"]] = r["pv"]
        else:
            for r in self.query_all("SELECT product_id, version, release_id, display_name, "
                                    "payload_version FROM releases " + rel_where
                                    + " ORDER BY payload_version DESC", rel_params):
                releases.setdefault(r["product_id"], {}).setdefault(
                    r["version"],
                    {"release_id": r["release_id"], "display_name": r["display_name"] or ""})
                newest_pv.setdefault(r["product_id"], r["payload_version"])
        # adoption: devices at or past that newest release. Counted here rather than left
        # to the caller -- by_version is keyed by version STRING, which cannot be compared.
        by_pv: dict[int, list] = {}
        for r in self.query_all("SELECT product_id, current_payload_version AS pv, COUNT(*) AS n "
                                "FROM devices " + where + " GROUP BY product_id, pv", params):
            by_pv.setdefault(r["product_id"], []).append((r["pv"], r["n"]))
        products: dict[str, dict] = {}
        total = fell_back = unconfirmed = up_to_date = measured = 0
        for r in self.query_all(
                "SELECT product_id, COUNT(*) AS n, "
                "SUM(CASE WHEN fallback_reason IS NOT NULL THEN 1 ELSE 0 END) AS fb, "
                "SUM(CASE WHEN confirmed = 0 THEN 1 ELSE 0 END) AS uc "
                "FROM devices " + where + " GROUP BY product_id", params):
            pid = r["product_id"]
            npv = newest_pv.get(pid)
            up = (sum(n for pv, n in by_pv.get(pid, []) if pv is not None and pv >= npv)
                  if npv is not None else 0)
            if not totals:
                products[str(pid)] = {
                    "total": r["n"], "by_version": by_version.get(pid, {}),
                    "by_fallback": by_fallback.get(pid, {}),
                    "by_cohort": by_cohort.get(pid, {}),
                    "releases": releases.get(pid, {}),
                    "fell_back": r["fb"], "unconfirmed": r["uc"], "up_to_date": up,
                    "measured": r["n"] if npv is not None else 0}
            total += r["n"]
            fell_back += r["fb"]
            unconfirmed += r["uc"]
            up_to_date += up
            measured += r["n"] if npv is not None else 0
        return {"total": total, "fell_back": fell_back, "unconfirmed": unconfirmed,
                "up_to_date": up_to_date, "measured": measured, "products": products}

    def list_cohorts(self, product_id: int | None = None, account_id=None,
                     products=None) -> list[dict]:
        """The cohorts in use, each with its device count AND its per-product breakdown --
        a cohort name spans products (it is a label on devices), so the flat count alone
        hides composition a `(product, cohort)`-targeted rollout or pin cares about."""
        where, params = _scope(account_id, product_id, products)
        rows = self.query_all(
            "SELECT cohort, product_id, COUNT(*) AS devices FROM devices " + where
            + " GROUP BY cohort, product_id ORDER BY cohort, product_id", params)
        out: dict[str, dict] = {}
        for r in rows:
            c = out.setdefault(r["cohort"], {"cohort": r["cohort"], "devices": 0,
                                             "by_product": {}})
            c["devices"] += r["devices"]
            c["by_product"][str(r["product_id"])] = r["devices"]
        # declared-but-empty labels (created ahead of their first device) show with 0
        # NOT product-scoped: a declared cohort label has no product column. The device
        # counts above are what a limited token must not see beyond its products, and
        # those are already filtered; a label is just a name.
        dwhere, dparams = _scope(account_id)
        for r in self.query_all("SELECT cohort FROM cohorts " + dwhere + " ORDER BY cohort",
                                dparams):
            out.setdefault(r["cohort"], {"cohort": r["cohort"], "devices": 0, "by_product": {}})
        # pins, per (product, cohort): the holdback half of what a cohort is for
        for c in out.values():
            c["pins"] = {}
        for r in self.query_all("SELECT product_id, cohort, release_id FROM cohort_pins "
                                + where, params):
            c = out.setdefault(r["cohort"], {"cohort": r["cohort"], "devices": 0,
                                             "by_product": {}, "pins": {}})
            c["pins"][str(r["product_id"])] = r["release_id"]
        return sorted(out.values(), key=lambda c: c["cohort"])

    COHORT_SORTS = {"cohort": lambda c: (c["cohort"] == "__default__", c["cohort"].lower()),
                    "devices": lambda c: c["devices"],
                    "products": lambda c: len(c["by_product"]),
                    "pins": lambda c: len(c["pins"])}

    def page_cohorts(self, product_id=None, account_id=None, sort=None, direction=None,
                     products=None,
                     limit=None, offset=0) -> tuple[list[dict], int]:
        """``list_cohorts`` on the list contract: (page, total). The set is small and
        aggregated, so it is sorted here, with the same whitelist idea as the SQL lists."""
        rows = self.list_cohorts(product_id, account_id=account_id, products=products)
        key = self.COHORT_SORTS.get(sort or "cohort", self.COHORT_SORTS["cohort"])
        rows.sort(key=key, reverse=(str(direction).lower() == "desc"))
        total = len(rows)
        rows = rows[offset: offset + limit] if limit is not None else rows[offset:]
        return rows, total

    def assign_cohort(self, device_ids: list, cohort: str, account_id=None) -> int:
        """Move the given (already-registered) devices into ``cohort``; returns how many existed.
        Scoped to ``account_id`` when given, so an admin can't reassign another account's device."""
        if not device_ids:
            return 0
        placeholders = ",".join("?" for _ in device_ids)
        sql = "UPDATE devices SET cohort = ? WHERE device_id IN (" + placeholders + ")"
        params = [cohort, *device_ids]
        if account_id is not None:
            sql += " AND account_id = ?"
            params.append(account_id)
        self.declare_cohort(cohort, account_id)
        return self.execute(sql, tuple(params)).rowcount

    def assign_cohort_product(self, product_id: int, cohort: str, account_id=None) -> int:
        """Move EVERY device of ``product_id`` into ``cohort``; returns how many moved.
        The bulk selector beside per-id ``assign_cohort`` -- a cohort stays a per-device
        label, this just sets it fleet-wide in one statement. Same account scoping."""
        sql = "UPDATE devices SET cohort = ? WHERE product_id = ?"
        params = [cohort, product_id]
        if account_id is not None:
            sql += " AND account_id = ?"
            params.append(account_id)
        self.declare_cohort(cohort, account_id)
        return self.execute(sql, tuple(params)).rowcount

    # --- version pins (device / cohort, override rollouts) ----------------------------------

    def set_device_name(self, device_id: str, name: str) -> None:
        """Set the operator-facing display name (empty = clear). A label only."""
        self.execute("UPDATE devices SET display_name = ? WHERE device_id = ?",
                     (name, device_id))

    def set_release_name(self, release_id: str, name: str) -> None:
        """Set a release's display name (empty = clear). A label only."""
        self.execute("UPDATE releases SET display_name = ? WHERE release_id = ?",
                     (name, release_id))

    def set_rollout_name(self, rollout_id: str, name: str) -> None:
        """Set a rollout's display name (empty = clear). A label only."""
        self.execute("UPDATE rollouts SET display_name = ? WHERE rollout_id = ?",
                     (name, rollout_id))

    # --- advisories (CVE monitoring) --------------------------------------------------------

    def releases_with_devices(self, account_id: str = "") -> list[dict]:
        """The releases the scanner must cover: every release some device is RUNNING,
        plus every release an active rollout is still offering. A release nobody runs
        and nobody offers needs no scan."""
        rows = self.query_all(
            "SELECT DISTINCT r.* FROM releases r JOIN devices d "
            "ON d.product_id = r.product_id AND d.current_version = r.version "
            "AND d.account_id = r.account_id WHERE r.account_id = ? "
            "UNION "
            "SELECT DISTINCT r.* FROM releases r JOIN rollouts ro "
            "ON ro.release_id = r.release_id AND ro.state = 'active' "
            "WHERE r.account_id = ?", (account_id, account_id))
        out = [_d(r) for r in rows]
        for r in out:
            r["representations"] = json.loads(r["representations"])
        return out

    def upsert_advisories(self, release_id: str, findings: list[dict],
                          account_id: str = "") -> dict:
        """Reconcile one release's scan result: new findings inserted (first_seen=now),
        repeats refreshed (last_seen), and active rows the scan no longer reports
        cleared. Returns {new: [...], cleared: n} -- `new` is what notification edges on."""
        now = _now_iso()
        current = {(a["vuln_id"], a["component"]): a
                   for a in self.query_all(
                       "SELECT * FROM advisories WHERE release_id = ? "
                       "AND cleared_at IS NULL", (release_id,))}
        new = []
        seen = set()
        for f in findings:
            key = (f["vuln_id"], f["component"])
            seen.add(key)
            if key in current:
                self.execute(
                    "UPDATE advisories SET last_seen = ?, severity = ?, summary = ? "
                    "WHERE release_id = ? AND vuln_id = ? AND component = ?",
                    (now, f.get("severity", "unknown"), f.get("summary", ""),
                     release_id, f["vuln_id"], f["component"]))
            else:
                self.execute(
                    "INSERT OR REPLACE INTO advisories (release_id, vuln_id, component, "
                    "version, severity, summary, first_seen, last_seen, cleared_at, "
                    "account_id) VALUES (?,?,?,?,?,?,?,?,NULL,?)",
                    (release_id, f["vuln_id"], f["component"], f.get("version", ""),
                     f.get("severity", "unknown"), f.get("summary", ""), now, now,
                     account_id))
                new.append(dict(f, release_id=release_id))
        cleared = 0
        for key, row in current.items():
            if key not in seen:
                self.execute(
                    "UPDATE advisories SET cleared_at = ? WHERE release_id = ? "
                    "AND vuln_id = ? AND component = ?",
                    (now, release_id, key[0], key[1]))
                cleared += 1
        return {"new": new, "cleared": cleared}

    def releases_with_active_advisories(self, account_id: str = "") -> list[str]:
        """Release ids still carrying active findings -- the reconciliation set:
        a release that left rotation must have its findings cleared, not linger."""
        return [r["release_id"] for r in self.query_all(
            "SELECT DISTINCT release_id FROM advisories WHERE account_id = ? "
            "AND cleared_at IS NULL", (account_id,))]

    ADVISORY_SORTS = {
        "severity": ("CASE a.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' "
                     "THEN 2 WHEN 'low' THEN 3 ELSE 4 END"),
        "advisory": "a.vuln_id", "component": "a.component",
        "release": "COALESCE(NULLIF(r.display_name, ''), a.release_id) COLLATE NOCASE",
        "first_seen": "a.first_seen", "last_seen": "a.last_seen"}

    @staticmethod
    def _advisories_where(account_id, release_id, active_only) -> tuple[str, tuple]:
        sql = "WHERE a.account_id = ?"
        params: tuple = (account_id,)
        if release_id is not None:
            sql, params = sql + " AND a.release_id = ?", (*params, release_id)
        if active_only:
            sql += " AND a.cleared_at IS NULL"
        return sql, params

    def count_advisories(self, account_id: str = "", release_id=None, active_only=True) -> int:
        where, params = self._advisories_where(account_id, release_id, active_only)
        return self.query_one("SELECT COUNT(*) AS n FROM advisories a " + where, params)["n"]

    def list_advisories(self, account_id: str = "", release_id: str | None = None,
                        active_only: bool = True, sort=None, direction=None, limit=None,
                        offset: int = 0) -> list[dict]:
        """Each row carries ``release_name`` (the release's display name, '' if none) so
        a finding can be labelled without a second lookup."""
        where, params = self._advisories_where(account_id, release_id, active_only)
        sql = ("SELECT a.*, COALESCE(r.display_name, '') AS release_name FROM advisories a "
               "LEFT JOIN releases r ON r.release_id = a.release_id " + where
               + _order(sort, direction, self.ADVISORY_SORTS, "a.first_seen DESC, a.vuln_id",
                        "a.vuln_id"))
        sql, params = _limit(sql, params, limit, offset)
        return [_d(r) for r in self.query_all(sql, params)]

    def set_device_pin(self, device_id: str, release_id: str | None,
                       account_id: str = "") -> None:
        """Pin (or, with None, unpin) a device to a release.

        An intent about a device id, not a field on a fleet row -- so it can be recorded
        for a camera the server has never seen and is waiting when that camera first checks
        in. A platform claims hardware at the moment it ships, which is before anything has
        been powered on; as an UPDATE on `devices` this matched no rows and did nothing, so
        the claim had to wait for one check-in to create the row and then land on the next.
        """
        if release_id is None:
            self.execute("DELETE FROM device_pins WHERE device_id = ?", (device_id,))
            return
        self.execute(
            "INSERT INTO device_pins (device_id, release_id, account_id, pinned_at) "
            "VALUES (?,?,?,?) ON CONFLICT (device_id) DO UPDATE SET "
            "release_id = excluded.release_id, account_id = excluded.account_id, "
            "pinned_at = excluded.pinned_at",
            (device_id, release_id, account_id, _now_iso()))

    def get_device_pin(self, device_id: str) -> str | None:
        """The release this device is pinned to, whether or not it has ever checked in."""
        row = self.query_one("SELECT release_id FROM device_pins WHERE device_id = ?",
                             (device_id,))
        return row["release_id"] if row else None

    def device_pins_for(self, device_ids) -> dict:
        """``{device_id: release_id}`` for a page of devices -- one query per listing
        rather than one per row, bounded by the page the caller already read."""
        ids = list(device_ids)
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        return {r["device_id"]: r["release_id"] for r in self.query_all(
            "SELECT device_id, release_id FROM device_pins WHERE device_id IN (%s)" % marks,
            tuple(ids))}

    def set_cohort_pin(self, product_id: int, cohort: str, release_id: str | None,
                       account_id: str = "") -> None:
        if release_id is None:
            self.execute("DELETE FROM cohort_pins WHERE account_id = ? AND product_id = ? "
                         "AND cohort = ?", (account_id, product_id, cohort))
        else:
            self.execute(
                "INSERT INTO cohort_pins (account_id, product_id, cohort, release_id) VALUES (?,?,?,?) "
                "ON CONFLICT (account_id, product_id, cohort) DO UPDATE SET release_id = excluded.release_id",
                (account_id, product_id, cohort, release_id))

    def get_cohort_pin(self, product_id: int, cohort: str, account_id: str = "") -> str | None:
        row = self.query_one("SELECT release_id FROM cohort_pins WHERE account_id = ? "
                             "AND product_id = ? AND cohort = ?", (account_id, product_id, cohort))
        return row["release_id"] if row else None

    # --- deployments (explicit terminal outcome reports) ------------------------------------

    def record_deployment(self, *, device_id, release_id, product_id, status, reason=None,
                          account_id="") -> None:
        """Upsert the authoritative outcome for (device_id, release_id) -- one row per pair."""
        self.execute(
            "INSERT INTO deployments (device_id, release_id, product_id, status, reason, "
            "account_id, reported_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT (device_id, release_id) "
            "DO UPDATE SET status = excluded.status, reason = excluded.reason, "
            "account_id = excluded.account_id, reported_at = excluded.reported_at",
            (device_id, release_id, product_id, status, reason, account_id, _now_iso()))

    def installs_by_day(self, days: int = 14, product_id=None, account_id=None,
                        products=None) -> dict:
        """Installs and failures per UTC day, oldest first, over the last ``days``.

        Every day in the window is present, zero-filled: the caller draws the series
        without inventing the gaps, and a quiet Sunday is a zero column rather than a
        missing one that silently shortens the chart.

        It counts DEPLOYMENT ROWS -- one per (device, release) pair -- on the day they
        were last reported. A device reporting the same release twice moves its own row
        instead of adding one, so this is outcomes as they landed, which is what a fleet
        chart wants and all the table can honestly answer."""
        where, params = _scope(account_id, product_id, products)
        start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
        where = _and(where, "reported_at >= ?")     # ISO text: a date prefix compares
        params = (*params, start.isoformat())
        by: dict[str, dict] = {}
        for r in self.query_all("SELECT substr(reported_at, 1, 10) AS day, status, "
                                "COUNT(*) AS n FROM deployments " + where
                                + " GROUP BY substr(reported_at, 1, 10), status", params):
            by.setdefault(r["day"], {})[r["status"]] = r["n"]
        out, installed, failed = [], 0, 0
        for i in range(days):
            day = (start + timedelta(days=i)).isoformat()
            got = by.get(day) or {}
            ins, fail = int(got.get("installed") or 0), int(got.get("failed") or 0)
            out.append({"day": day, "installed": ins, "failed": fail})
            installed += ins
            failed += fail
        return {"days": out, "installed": installed, "failed": failed}

    def deployment_counts(self, release_id: str) -> dict:
        """Reported {installed, failed} counts for a release (from explicit /feedback)."""
        rows = self.query_all(
            "SELECT status, COUNT(*) AS n FROM deployments WHERE release_id = ? GROUP BY status",
            (release_id,))
        by = {r["status"]: r["n"] for r in rows}
        return {"installed": by.get("installed", 0), "failed": by.get("failed", 0)}

    # --- accounts (tenants) -----------------------------------------------------------------

    def add_account(self, account_id: str, name: str, *, created_by: str = "",
                    client_ref: str = "") -> None:
        self.execute("INSERT INTO accounts (account_id, name, created_at, created_by, client_ref) "
                     "VALUES (?,?,?,?,?)",
                     (account_id, name, _now_iso(), created_by, client_ref))

    def next_publish_seq(self, account_id: str) -> int | None:
        """Allocate the account's next publish counter -- increment and return. None when
        there is no such account (a self-host's implicit ``''`` has no row to count in).

        Gaps are fine and expected: a build that fails after taking a number simply burns
        it. What must never happen is two builds taking the SAME number, which is why the
        increment is a single statement rather than a read followed by a write."""
        if not self.execute(
                "UPDATE accounts SET publish_seq = publish_seq + 1 WHERE account_id = ?",
                (account_id,)).rowcount:
            return None
        row = self.query_one("SELECT publish_seq FROM accounts WHERE account_id = ?",
                             (account_id,))
        return int(row["publish_seq"])

    def newest_publish_seq(self, product_id: int, account_id: str = "") -> int:
        """The highest publish counter already published for this product (0 if none)."""
        where, params = _scope(account_id, product_id)
        row = self.query_one("SELECT MAX(publish_seq) AS n FROM releases " + where, params)
        return int((row["n"] if row else None) or 0)

    def account_by_client_ref(self, created_by: str, client_ref: str) -> dict | None:
        """The account this operator already created under its own reference, or None.

        What makes creation safe to retry: the caller asks again with the same
        ``client_ref`` and gets the same account back rather than a duplicate."""
        return _d(self.query_one(
            "SELECT * FROM accounts WHERE created_by = ? AND client_ref = ? AND client_ref != ''",
            (created_by, client_ref)))

    def get_account(self, account_id: str) -> dict | None:
        return _d(self.query_one("SELECT * FROM accounts WHERE account_id = ?", (account_id,)))

    def delete_account(self, account_id: str, *, actor: str = "cli", via: dict | None = None) -> dict:
        """Remove a DEACTIVATED account and every row it owned -- tokens, products,
        releases, rollouts, cohorts and pins, deployments, devices and their bindings,
        advisories, webhooks and their deliveries -- in one transaction. The audit log
        is the exception: it is one hash chain for the whole server, so its rows are
        never deleted; the account's history stays readable and the deletion itself is
        appended. Returns the per-table counts and the artifact keys the caller removes
        from storage afterwards (rows first, so a storage hiccup can only ever leave an
        orphaned object, never a release row pointing at nothing)."""
        acct = self.get_account(account_id)
        if acct is None:
            raise ServerError("no such account", exit_code=1)
        if acct.get("active"):
            raise ServerError("%s is active; deactivate it first" % account_id, exit_code=1)
        keys: list[str] = []
        for r in self.query_all("SELECT release_id, manifest_key, image_key, sbom_key, "
                                "representations FROM releases WHERE account_id = ?",
                                (account_id,)):
            r = _d(r)
            keys += [k for k in (r["manifest_key"], r["image_key"], r.get("sbom_key")) if k]
            try:
                reps = json.loads(r.get("representations") or "[]")
            except ValueError:
                reps = []
            keys += ["artifacts/%s/%s" % (r["release_id"], rep["url"])
                     for rep in reps if isinstance(rep, dict) and rep.get("url")]
        stmts = (
            ("webhook_deliveries", "DELETE FROM webhook_deliveries WHERE webhook_id IN "
                                   "(SELECT webhook_id FROM webhooks WHERE account_id = ?)"),
            ("webhooks", "DELETE FROM webhooks WHERE account_id = ?"),
            ("advisories", "DELETE FROM advisories WHERE account_id = ?"),
            ("device_pins", "DELETE FROM device_pins WHERE account_id = ?"),
            ("cohort_pins", "DELETE FROM cohort_pins WHERE account_id = ?"),
            ("cohorts", "DELETE FROM cohorts WHERE account_id = ?"),
            ("deployments", "DELETE FROM deployments WHERE account_id = ?"),
            ("rollouts", "DELETE FROM rollouts WHERE account_id = ?"),
            ("releases", "DELETE FROM releases WHERE account_id = ?"),
            ("device_accounts", "DELETE FROM device_accounts WHERE account_id = ?"),
            ("devices", "DELETE FROM devices WHERE account_id = ?"),
            ("products", "DELETE FROM products WHERE account_id = ?"),
            ("admin_tokens", "DELETE FROM admin_tokens WHERE account_id = ?"),
            ("accounts", "DELETE FROM accounts WHERE account_id = ?"),
        )
        rows: dict[str, int] = {}
        with self._lock:
            cur = self._conn.cursor()
            for table, sql in stmts:
                self._run(cur, sql, (account_id,))
                rows[table] = max(0, cur.rowcount)
            self._conn.commit()
        keys = list(dict.fromkeys(keys))
        self.append_audit(actor=actor, action="account.delete", entity_type="account",
                          entity_id=account_id,
                          data={"name": acct.get("name", ""), "rows": rows,
                                "artifacts": len(keys), **(via or {})},
                          account_id=account_id)
        return {"rows": rows, "keys": keys}

    @staticmethod
    def _accounts_where(created_by, q, active=None) -> tuple[str, list]:
        conds, params = [], []
        if created_by is not None:
            conds.append("created_by = ?")
            params.append(created_by)
        if q:
            conds.append("(LOWER(name) LIKE ? OR account_id LIKE ? OR client_ref LIKE ?)")
            params += ["%" + q.lower() + "%", "%" + q + "%", "%" + q + "%"]
        if active is not None:
            conds.append("active = ?")
            params.append(1 if active else 0)
        return (" WHERE " + " AND ".join(conds)) if conds else "", params

    def list_accounts(self, created_by: str | None = None, q: str | None = None,
                      limit: int | None = None, offset: int = 0,
                      active: bool | None = None) -> list[dict]:
        """Every account, or only the ones ``created_by`` this operator credential.

        None is the server operator's view. A string is a tenant-of-a-tenant view: a
        platform reselling this service sees the customers it provisioned and not that
        anyone else exists. ``q`` matches the name, the id or the client reference;
        ``limit``/``offset`` page (no limit = all of them, the CLI's whole listing)."""
        where, params = self._accounts_where(created_by, q, active)
        sql = "SELECT * FROM accounts" + where + " ORDER BY created_at, account_id"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return [_d(r) for r in self.query_all(sql, tuple(params))]

    def count_accounts(self, created_by: str | None = None, q: str | None = None,
                       active: bool | None = None) -> int:
        where, params = self._accounts_where(created_by, q, active)
        return self.query_one("SELECT COUNT(*) AS n FROM accounts" + where, tuple(params))["n"]

    def account_counts(self, account_ids) -> dict:
        """``{account_id: {devices, releases, active_rollouts, last_seen}}`` -- the numbers
        an operator's directory shows beside each account, in four grouped reads rather
        than four per row."""
        ids = [a for a in account_ids]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        out = {a: {"devices": 0, "releases": 0, "active_rollouts": 0, "last_seen": None}
               for a in ids}
        for r in self.query_all("SELECT account_id, COUNT(*) AS n, MAX(last_seen) AS seen "
                                "FROM devices WHERE account_id IN (%s) GROUP BY account_id"
                                % marks, tuple(ids)):
            out[r["account_id"]].update(devices=r["n"], last_seen=r["seen"])
        for r in self.query_all("SELECT account_id, COUNT(*) AS n FROM releases "
                                "WHERE account_id IN (%s) GROUP BY account_id" % marks,
                                tuple(ids)):
            out[r["account_id"]]["releases"] = r["n"]
        for r in self.query_all("SELECT account_id, COUNT(*) AS n FROM rollouts WHERE "
                                "state = 'active' AND account_id IN (%s) GROUP BY account_id"
                                % marks, tuple(ids)):
            out[r["account_id"]]["active_rollouts"] = r["n"]
        return out

    def account_name_exists(self, name: str, except_id: str | None = None,
                            created_by: str | None = None) -> bool:
        """Whether another account already uses ``name`` (case-insensitive). ``except_id``
        excludes one account (so a rename to the same name is fine).

        ``created_by`` narrows the question to one operator's own accounts, which is the
        only scope in which it is a real answer: two platforms reselling this server have
        no reason to share a namespace, and a 409 that crosses between them both blocks a
        legitimate name and reveals that the other platform has a customer by that name."""
        sql = "SELECT 1 FROM accounts WHERE LOWER(name) = LOWER(?) AND account_id <> ?"
        params: list = [name, except_id or ""]
        if created_by is not None:
            sql += " AND created_by = ?"
            params.append(created_by)
        return self.query_one(sql, tuple(params)) is not None

    def rename_account(self, account_id: str, name: str) -> None:
        self.execute("UPDATE accounts SET name = ? WHERE account_id = ?", (name, account_id))

    def set_device_limit(self, account_id: str, limit: int | None) -> None:
        self.execute("UPDATE accounts SET device_limit = ? WHERE account_id = ?",
                     (limit, account_id))

    def device_count(self, account_id: str) -> int:
        return self.query_one("SELECT COUNT(*) AS n FROM devices WHERE account_id = ?",
                              (account_id,))["n"]

    def limit_refusal_seen(self, account_id: str, device_id: str) -> bool:
        """Whether this device's limit refusal is already in the audit (one row per id)."""
        return self.query_one(
            "SELECT 1 AS x FROM audit WHERE account_id = ? AND action = 'device.refused' "
            "AND entity_id = ? LIMIT 1", (account_id, device_id)) is not None

    def over_device_limit(self, account_id: str) -> bool:
        """Whether registering ONE MORE device would exceed the account's limit."""
        acct = self.get_account(account_id) if account_id else None
        limit = (acct or {}).get("device_limit")
        return limit is not None and self.device_count(account_id) >= limit

    def set_account_active(self, account_id: str, active: bool) -> None:
        self.execute("UPDATE accounts SET active = ? WHERE account_id = ?",
                     (1 if active else 0, account_id))

    # --- admin tokens (stored hashed) -------------------------------------------------------

    def add_token(self, token_hash: str, name: str, scopes: list[str], account_id: str = "",
                  products=()) -> None:
        """Store a token. ``products`` limits it to those product ids; empty is the whole
        account, which is what an unscoped token gets."""
        self.execute("INSERT INTO admin_tokens (token_hash, name, scopes, created_at, account_id, "
                     "products) VALUES (?,?,?,?,?,?)",
                     (token_hash, name, ",".join(scopes), _now_iso(), account_id,
                      ",".join(str(int(p)) for p in products)))

    def get_token(self, token_hash: str) -> dict | None:
        r = _d(self.query_one("SELECT * FROM admin_tokens WHERE token_hash = ?", (token_hash,)))
        if r is not None:
            r["scopes"] = r["scopes"].split(",") if r["scopes"] else []
            raw = r.get("products") or ""
            r["products"] = [int(p) for p in raw.split(",") if p]
        return r

    def token_name_in_use(self, account_id: str, name: str) -> bool:
        """Whether an account already has a LIVE token called ``name`` (revoked ones free the
        name). Names are what the audit log records as the actor, so two live tokens with
        one name would be indistinguishable there."""
        return self.query_one("SELECT 1 FROM admin_tokens WHERE account_id = ? AND name = ? "
                              "AND revoked = 0", (account_id, name)) is not None

    def revoke_token(self, token_hash: str) -> None:
        self.execute("UPDATE admin_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))

    def list_tokens(self, account_id=None) -> list[dict]:
        where, params = ("WHERE account_id = ?", (account_id,)) if account_id is not None else ("", ())
        rows = [_d(r) for r in self.query_all(
            "SELECT token_hash, name, scopes, products, account_id, created_at, revoked "
            "FROM admin_tokens " + where + " ORDER BY created_at", params)]
        for r in rows:
            r["scopes"] = r["scopes"].split(",") if r["scopes"] else []
            # the allow-list belongs in a listing: `scopes` alone cannot tell you whether
            # a credential is the whole account or one customer's product
            r["products"] = [int(p) for p in (r.get("products") or "").split(",") if p]
        return rows

    def revoke_account_tokens(self, account_id: str) -> int:
        """Revoke every live token for an account (the token half of deactivation). Returns count."""
        return self.execute("UPDATE admin_tokens SET revoked = 1 WHERE account_id = ? AND revoked = 0",
                            (account_id,)).rowcount

    def count_tokens(self) -> int:
        return self.query_one("SELECT COUNT(*) AS n FROM admin_tokens")["n"]

    # --- the hash-chained audit log ---------------------------------------------------------

    def append_audit(self, *, actor, action, entity_type=None, entity_id=None, data=None,
                     account_id="", product_id=None) -> int:
        """Append one entry. ``product_id`` is the product the act happened to, where the
        caller knows one -- it is what lets a product-limited credential read its own
        history, and it is stored beside the chain rather than inside it (see the v25
        migration).

        The read of the last entry and the insert of the next are ONE critical section under
        the store lock. The sequence is ``MAX(seq)+1`` and the entry hash chains onto the
        previous ``entry_hash``, so two appenders that each read the same last row would insert
        the same seq (which is UNIQUE -> IntegrityError -> a 500) and fork the chain. The server
        appends concurrently in practice -- a publish on the event-loop thread while a periodic
        `advisory.scan` runs in a worker thread (`asyncio.to_thread`) -- and that race surfaced as
        `UNIQUE constraint failed: audit.seq`. `query_one`/`execute` each take and release the lock
        on their own, leaving a gap between the read and the insert; holding the lock across both
        closes it. `_fan_out` re-acquires the lock, so it stays OUTSIDE this block."""
        ts = _now_iso()
        payload = json.dumps(data or {}, separators=(",", ":"), sort_keys=True)
        pid = None if product_id is None else int(product_id)
        with self._lock:
            cur = self._conn.cursor()
            self._run(cur, "SELECT seq, entry_hash FROM audit ORDER BY seq DESC LIMIT 1", ())
            last = cur.fetchone()
            seq = (last["seq"] + 1) if last else 1
            prev = last["entry_hash"] if last else ""
            entry = _audit_hash(prev, ts, actor, action, entity_type, entity_id, payload)
            self._run(cur,
                      "INSERT INTO audit (seq, ts, actor, action, entity_type, entity_id, data, "
                      "prev_hash, entry_hash, account_id, product_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (seq, ts, actor, action, entity_type, entity_id, payload, prev, entry,
                       account_id, pid))
            self._conn.commit()
        if account_id:
            self._fan_out(seq, action, account_id, ts)
        return seq

    # --- webhooks: the audit log's push side ------------------------------------------------

    @staticmethod
    def event_matches(patterns, action: str) -> bool:
        """``["*"]`` takes everything; ``"rollout.*"`` a family; ``"rollout.stop"`` one."""
        for pat in patterns:
            if pat == "*" or pat == action:
                return True
            if pat.endswith(".*") and action.startswith(pat[:-1]):
                return True
        return False

    def _fan_out(self, seq: int, action: str, account_id: str, ts: str) -> None:
        """One pending delivery per active endpoint of the account that subscribes to
        this action. Cheap (an insert each) so the audit write stays fast; the worker does
        the network."""
        for hook in self.list_webhooks(account_id, active_only=True):
            if self.event_matches(hook["events"], action):
                self.execute(
                    "INSERT INTO webhook_deliveries (delivery_id, webhook_id, audit_seq, event, "
                    "status, attempt, next_at, created_at) VALUES (?,?,?,?,'pending',0,?,?)",
                    ("dl_" + secrets.token_hex(8), hook["webhook_id"], seq, action, time.time(), ts))

    def _hook_row(self, row) -> dict | None:
        row = _d(row)
        if row is not None:
            row["events"] = json.loads(row["events"])
            row["active"] = int(row["active"])
        return row

    def add_webhook(self, *, account_id: str, url: str, events: list, secret: str,
                    description: str = "", created_by: str = "") -> dict:
        wid = "wh_" + secrets.token_hex(8)
        self.execute(
            "INSERT INTO webhooks (webhook_id, account_id, url, secret, events, active, description, "
            "created_at, created_by) VALUES (?,?,?,?,?,1,?,?,?)",
            (wid, account_id, url, secret, json.dumps(list(events)), description, _now_iso(), created_by))
        return self.get_webhook(wid, account_id)

    def get_webhook(self, webhook_id: str, account_id: str | None = None) -> dict | None:
        if account_id is None:
            return self._hook_row(self.query_one("SELECT * FROM webhooks WHERE webhook_id = ?", (webhook_id,)))
        return self._hook_row(self.query_one(
            "SELECT * FROM webhooks WHERE webhook_id = ? AND account_id = ?", (webhook_id, account_id)))

    def list_webhooks(self, account_id: str, active_only: bool = False) -> list:
        sql = "SELECT * FROM webhooks WHERE account_id = ?"
        if active_only:
            sql += " AND active = 1"
        return [self._hook_row(r) for r in self.query_all(sql + " ORDER BY created_at, webhook_id",
                                                          (account_id,))]

    def count_webhooks(self, account_id: str) -> int:
        return self.query_one("SELECT COUNT(*) AS n FROM webhooks WHERE account_id = ?",
                              (account_id,))["n"]

    def update_webhook(self, webhook_id: str, **fields) -> None:
        """url / events / active / description / secret; enabling clears the failure count
        and the disabled reason, so a repaired endpoint starts clean."""
        sets, params = [], []
        for k, v in fields.items():
            if k == "events":
                v = json.dumps(list(v))
            sets.append(f"{k} = ?")
            params.append(v)
        if fields.get("active") == 1:
            sets += ["failures = 0", "disabled_reason = ''"]
        self.execute(f"UPDATE webhooks SET {', '.join(sets)} WHERE webhook_id = ?",
                     (*params, webhook_id))

    def delete_webhook(self, webhook_id: str) -> None:
        self.execute("DELETE FROM webhook_deliveries WHERE webhook_id = ?", (webhook_id,))
        self.execute("DELETE FROM webhooks WHERE webhook_id = ?", (webhook_id,))

    def enqueue_delivery(self, webhook_id: str, audit_seq: int, event: str) -> str:
        """A delivery by hand -- the endpoint's test ping, or an operator's retry of an
        entry the worker gave up on."""
        did = "dl_" + secrets.token_hex(8)
        self.execute(
            "INSERT INTO webhook_deliveries (delivery_id, webhook_id, audit_seq, event, status, "
            "attempt, next_at, created_at) VALUES (?,?,?,?,'pending',0,?,?)",
            (did, webhook_id, audit_seq, event, time.time(), _now_iso()))
        return did

    def claim_due_deliveries(self, now: float, limit: int = 50, lease_s: float = 60.0) -> list:
        """Pending deliveries whose time has come, leased to this worker for ``lease_s``
        so a second process (or a slow attempt) never sends the same one twice."""
        rows = self.query_all(
            "SELECT * FROM webhook_deliveries WHERE status = 'pending' AND next_at <= ? "
            "AND claimed_until <= ? ORDER BY next_at, audit_seq LIMIT ?", (now, now, limit))
        out = []
        for r in rows:
            r = _d(r)
            self.execute("UPDATE webhook_deliveries SET claimed_until = ? WHERE delivery_id = ? "
                         "AND claimed_until <= ?", (now + lease_s, r["delivery_id"], now))
            out.append(r)
        return out

    def finish_delivery(self, delivery_id: str, *, status: str, attempt: int, code, error: str,
                        next_at: float) -> None:
        self.execute(
            "UPDATE webhook_deliveries SET status = ?, attempt = ?, last_code = ?, last_error = ?, "
            "next_at = ?, claimed_until = 0, delivered_at = ? WHERE delivery_id = ?",
            (status, attempt, code, error[:200], next_at,
             _now_iso() if status == "delivered" else None, delivery_id))

    def note_webhook_result(self, webhook_id: str, *, ok: bool, code) -> int:
        """Bump or reset the consecutive-failure count; returns the count after."""
        if ok:
            self.execute("UPDATE webhooks SET failures = 0, last_delivery_at = ?, last_status = ? "
                         "WHERE webhook_id = ?", (_now_iso(), code, webhook_id))
            return 0
        self.execute("UPDATE webhooks SET failures = failures + 1, last_delivery_at = ?, "
                     "last_status = ? WHERE webhook_id = ?", (_now_iso(), code, webhook_id))
        return self.query_one("SELECT failures FROM webhooks WHERE webhook_id = ?",
                              (webhook_id,))["failures"]

    def disable_webhook(self, webhook_id: str, reason: str) -> None:
        self.execute("UPDATE webhooks SET active = 0, disabled_reason = ? WHERE webhook_id = ?",
                     (reason, webhook_id))
        self.execute("UPDATE webhook_deliveries SET status = 'dead' WHERE webhook_id = ? "
                     "AND status = 'pending'", (webhook_id,))

    def list_deliveries(self, webhook_id: str, limit: int = 50, offset: int = 0,
                        status: str | None = None) -> tuple[list, int]:
        where, params = "WHERE webhook_id = ?", [webhook_id]
        if status:
            where += " AND status = ?"
            params.append(status)
        rows = self.query_all(
            f"SELECT delivery_id, webhook_id, audit_seq, event, status, attempt, next_at, "
            f"last_code, last_error, created_at, delivered_at FROM webhook_deliveries {where} "
            f"ORDER BY created_at DESC, delivery_id DESC LIMIT ? OFFSET ?", (*params, limit, offset))
        total = self.query_one(f"SELECT COUNT(*) AS n FROM webhook_deliveries {where}",
                               tuple(params))["n"]
        return [_d(r) for r in rows], total

    def get_delivery(self, delivery_id: str, webhook_id: str) -> dict | None:
        return _d(self.query_one("SELECT * FROM webhook_deliveries WHERE delivery_id = ? "
                                 "AND webhook_id = ?", (delivery_id, webhook_id)))

    def retry_delivery(self, delivery_id: str) -> None:
        self.execute("UPDATE webhook_deliveries SET status = 'pending', next_at = ?, claimed_until = 0 "
                     "WHERE delivery_id = ?", (time.time(), delivery_id))

    def get_audit_entry(self, seq: int) -> dict | None:
        row = _d(self.query_one("SELECT * FROM audit WHERE seq = ?", (seq,)))
        if row is not None:
            row["data"] = json.loads(row["data"] or "{}")
        return row

    AUDIT_SORTS = {"when": "seq", "action": "action", "actor": "actor", "entity": "entity_id"}

    @staticmethod
    def _audit_where(since_seq, account_id, entity_id, action_not=None,
                     action=None, products=None) -> tuple[str, list]:
        sql = "WHERE seq > ?"
        params = [since_seq]
        if products is not None:
            # A product-limited credential sees its products' history and nothing else --
            # including nothing of the rows that belong to no product (account and token
            # administration), which are the account's business, not its customer's. An
            # empty allow-list is a token scoped to nothing, and `IN ()` is a syntax error.
            if not products:
                sql += " AND 1 = 0"
            else:
                sql += " AND product_id IN (%s)" % ",".join("?" * len(products))
                params.extend(int(pid) for pid in products)
        if account_id is not None:
            sql += " AND account_id = ?"
            params.append(account_id)
        if entity_id is not None:
            sql += " AND entity_id = ?"
            params.append(entity_id)
        if action is not None:                       # e.g. count only device.refused
            sql += " AND action = ?"
            params.append(action)
        if action_not is not None:                   # e.g. hide the daily advisory.scan rows
            sql += " AND action != ?"
            params.append(action_not)
        return sql, params

    def recent_activity(self, limit: int = 6, account_id=None, action_not=None) -> list[dict]:
        """What has been happening, GROUPED: the newest event of each (action, actor),
        carrying how many times that pair appears in the log, newest group first.

        A plain tail of the audit log is not this. Onboarding four hundred devices
        writes four hundred consecutive rows, so the last N of anything is that one act,
        four hundred times, and everything before it is invisible -- the deeper a caller
        pages, the more of the same it gets. Grouping is the only way to answer "what has
        been going on" in a fixed number of rows, and SQL is where it belongs: the group
        is computed over the whole log, not over whatever window a caller happened to
        read."""
        where, params = self._audit_where(0, account_id, None, action_not, None)
        groups = self.query_all(
            "SELECT action, actor, COUNT(*) AS n, MAX(seq) AS newest FROM audit " + where
            + " GROUP BY action, actor ORDER BY MAX(seq) DESC LIMIT ?", (*params, limit))
        if not groups:
            return []
        seqs = [g["newest"] for g in groups]
        marks = ",".join("?" * len(seqs))
        rows = {r["seq"]: _d(r) for r in self.query_all(
            "SELECT * FROM audit WHERE seq IN (%s)" % marks, tuple(seqs))}
        out = []
        for g in groups:                             # the newest row of each group, + count
            row = rows[g["newest"]]
            row["data"] = json.loads(row["data"])
            row["count"] = g["n"]
            out.append(row)
        return out

    def count_audit(self, since_seq: int = 0, account_id=None, entity_id=None,
                    action_not=None, action=None, products=None) -> int:
        where, params = self._audit_where(since_seq, account_id, entity_id, action_not, action,
                                          products)
        return self.query_one("SELECT COUNT(*) AS n FROM audit " + where, tuple(params))["n"]

    def read_audit(self, limit: int = 100, since_seq: int = 0, account_id=None,
                   entity_id: str | None = None, newest: bool = False, sort=None,
                   direction=None, offset: int = 0, action_not=None,
                   action=None, products=None) -> list[dict]:
        """``newest`` flips the window to the most RECENT events (a history view);
        the default keeps append order (a log tail via ``since``). ``sort``/``direction``
        (when/action/actor/entity) generalise both; ``offset`` pages."""
        where, params = self._audit_where(since_seq, account_id, entity_id, action_not, action,
                                          products)
        sql = "SELECT * FROM audit " + where
        if sort in self.AUDIT_SORTS:
            sql += _order(sort, direction, self.AUDIT_SORTS, "seq", "seq")
        else:
            sql += " ORDER BY seq DESC" if newest else " ORDER BY seq"
        rows = [_d(r) for r in self.query_all(sql + " LIMIT ? OFFSET ?", (*params, limit, offset))]
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    def audit_chain_ok(self) -> bool:
        """Whether the audit hash-chain is intact (tamper check)."""
        prev = ""
        for r in self.query_all("SELECT * FROM audit ORDER BY seq"):
            expect = _audit_hash(prev, r["ts"], r["actor"], r["action"], r["entity_type"],
                                 r["entity_id"], r["data"])
            if r["prev_hash"] != prev or r["entry_hash"] != expect:
                return False
            prev = r["entry_hash"]
        return True

    def close(self) -> None:
        self._conn.close()


class SqliteMetadataStore(SqlMetadataStore):
    paramstyle = "?"

    def __init__(self, path: str):
        import sqlite3
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        super().__init__(conn)


class PostgresMetadataStore(SqlMetadataStore):
    paramstyle = "%s"

    _POSTGRES_ONLY = "-- postgres: "

    def _dialect(self, stmt: str) -> str:
        """Run the Postgres half of a dialect-split migration step (see the base)."""
        return (stmt[len(self._POSTGRES_ONLY):]
                if stmt.startswith(self._POSTGRES_ONLY) else stmt)

    def __init__(self, dsn: str, connect=None):
        super().__init__((connect or self._default_connect(dsn))())

    # A schema change takes an exclusive table lock. A connection that read the table
    # and never ended its transaction ("idle in transaction" -- what this store's reads
    # did before 2026-09-13, and what any leaked client transaction does) holds a share
    # lock indefinitely, and the migration -- so the deploy -- waits behind it forever
    # with nothing in the log. Two guards: clear such backends of THIS database first
    # (same role, so permitted; only ones idle for a while, never a transaction in
    # flight), then cap the lock wait so a still-blocked migration fails loudly.
    _STALE_IDLE = "5 seconds"
    _LOCK_TIMEOUT = "30s"

    def _before_migrations(self) -> None:
        n = self.query_one(
            "SELECT COUNT(*) AS n FROM (SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND state = 'idle in transaction' "
            "AND state_change < now() - interval '%s') AS t" % self._STALE_IDLE)["n"]
        if n:
            print("migrate: ended %d idle-in-transaction connection(s) holding locks" % n,
                  file=sys.stderr, flush=True)
        self.execute("SET lock_timeout = '%s'" % self._LOCK_TIMEOUT)

    def _after_migrations(self) -> None:
        self.execute("RESET lock_timeout")

    @staticmethod
    def _default_connect(dsn: str):
        try:
            import psycopg
        except ImportError:
            raise ServerError("the postgres backend needs psycopg -- "
                              "pip install openmv-ota[server-postgres]", exit_code=2) from None
        from psycopg.rows import dict_row                          # pragma: no cover
        return lambda: psycopg.connect(dsn, row_factory=dict_row)  # pragma: no cover


def _sqlite_path(url: str) -> str:
    """The filesystem path (or ``:memory:``) from a ``sqlite:///…`` URL."""
    rest = url[len("sqlite://"):]
    return rest[1:] if rest.startswith("/") else rest


def build_metastore(settings) -> SqlMetadataStore:
    """The metadata store for ``settings.database_url`` (``sqlite:///…`` | ``postgres[ql]://…``)."""
    url = settings.database_url
    if url.startswith("sqlite:"):
        return SqliteMetadataStore(_sqlite_path(url))
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresMetadataStore(url)
    raise ServerError("unsupported database_url: %r" % url, exit_code=2)
