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
import threading
from datetime import datetime, timezone

from .errors import ServerError


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _d(row) -> dict | None:
    return dict(row) if row is not None else None


def _scope(account_id=None, product_id=None) -> tuple[str, tuple]:
    """A ``WHERE`` clause + params for the optional (account_id, product_id) filters -- the
    building block for account-scoped admin reads. Either/both may be None (no filter)."""
    conds, params = [], []
    if account_id is not None:
        conds.append("account_id = ?")
        params.append(account_id)
    if product_id is not None:
        conds.append("product_id = ?")
        params.append(product_id)
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
            release_id TEXT PRIMARY KEY, product_id INTEGER NOT NULL, product TEXT,
            version TEXT NOT NULL, payload_version INTEGER NOT NULL,
            min_platform_version INTEGER NOT NULL DEFAULT 0,
            image_sha256 TEXT NOT NULL, image_size INTEGER NOT NULL, representations TEXT NOT NULL,
            manifest_key TEXT NOT NULL, image_key TEXT NOT NULL, delta_key TEXT,
            key_id INTEGER, uploaded_by TEXT, uploaded_at TEXT NOT NULL)""",
        """CREATE TABLE rollouts (
            rollout_id TEXT PRIMARY KEY, release_id TEXT NOT NULL, product_id INTEGER NOT NULL,
            cohort TEXT NOT NULL, percent REAL NOT NULL, state TEXT NOT NULL,
            failure_threshold REAL NOT NULL DEFAULT 0.05, attempted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE devices (
            device_id TEXT PRIMARY KEY, product_id INTEGER NOT NULL, board TEXT,
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
            device_id TEXT NOT NULL, release_id TEXT NOT NULL, product_id INTEGER NOT NULL,
            status TEXT NOT NULL, reason TEXT, reported_at TEXT NOT NULL,
            PRIMARY KEY (device_id, release_id))""",
        "CREATE INDEX idx_deployments_release ON deployments (release_id, status)",
    ],
    [   # v3 -- version pins: force a specific device or cohort onto a release, overriding rollouts.
        "ALTER TABLE devices ADD COLUMN pinned_release_id TEXT",
        """CREATE TABLE cohort_pins (
            product_id INTEGER NOT NULL, cohort TEXT NOT NULL, release_id TEXT NOT NULL,
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
            account_id TEXT NOT NULL DEFAULT '', product_id INTEGER NOT NULL, cohort TEXT NOT NULL,
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

    def execute(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql(sql), params)
            self._conn.commit()
            return cur

    def query_one(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql(sql), params)
            return cur.fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql(sql), params)
            return list(cur.fetchall())

    def migrate(self) -> int:
        """Create the ``meta`` table and apply any migrations past the recorded ``schema_version``.
        Returns the resulting schema version. Idempotent."""
        self.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        current = int(self.get_meta("schema_version") or 0)
        for version, statements in enumerate(_MIGRATIONS, start=1):
            if version > current:
                for stmt in statements:
                    self.execute(stmt)
                current = version
        self.set_meta("schema_version", str(current))
        return current

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
                    account_id="", dev=0) -> None:
        self.execute(
            "INSERT INTO releases (release_id, product_id, product, version, payload_version, "
            "min_platform_version, image_sha256, image_size, representations, manifest_key, "
            "image_key, delta_key, key_id, uploaded_by, uploaded_at, account_id, dev) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (release_id, product_id, product, version, payload_version, min_platform_version,
             image_sha256, image_size, json.dumps(representations), manifest_key, image_key,
             delta_key, key_id, uploaded_by, _now_iso(), account_id, dev))

    def get_release(self, release_id: str) -> dict | None:
        r = _d(self.query_one("SELECT * FROM releases WHERE release_id = ?", (release_id,)))
        if r is not None:
            r["representations"] = json.loads(r["representations"])
        return r

    def list_releases(self, product_id=None, account_id=None, limit=None, offset=0) -> list[dict]:
        where, params = _scope(account_id, product_id)
        sql = "SELECT * FROM releases " + where + " ORDER BY payload_version DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (*params, limit, offset)
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
                    failure_threshold=0.05, account_id="") -> None:
        now = _now_iso()
        self.execute(
            "INSERT INTO rollouts (rollout_id, release_id, product_id, cohort, percent, state, "
            "failure_threshold, created_at, updated_at, account_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rollout_id, release_id, product_id, cohort, percent, state, failure_threshold, now,
             now, account_id))

    def get_rollout(self, rollout_id: str) -> dict | None:
        return _d(self.query_one("SELECT * FROM rollouts WHERE rollout_id = ?", (rollout_id,)))

    def active_rollout(self, product_id: int, cohort: str, account_id: str = "") -> dict | None:
        return _d(self.query_one(
            "SELECT * FROM rollouts WHERE account_id = ? AND product_id = ? AND cohort = ? "
            "AND state = 'active' ORDER BY created_at DESC LIMIT 1", (account_id, product_id, cohort)))

    def list_rollouts(self, product_id: int | None = None, account_id=None, limit=None,
                      offset=0) -> list[dict]:
        where, params = _scope(account_id, product_id)
        sql = "SELECT * FROM rollouts " + where + " ORDER BY created_at DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (*params, limit, offset)
        return [_d(r) for r in self.query_all(sql, params)]

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
                      streams=None) -> None:
        now = _now_iso()
        if self.query_one("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)) is None:
            self.execute(
                "INSERT INTO devices (device_id, product_id, board, cohort, current_version, "
                "current_payload_version, slot, representation, fallback_reason, confirmed, "
                "last_offered_release_id, registrar_ref, account_id, streams, first_seen, "
                "last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (device_id, product_id, board, cohort, current_version, current_payload_version,
                 slot, representation, fallback_reason, confirmed, last_offered_release_id,
                 registrar_ref, account_id, ",".join(streams or ()), now, now))
        else:                                               # cohort is admin-controlled, not by check-in
            self.execute(
                "UPDATE devices SET product_id = ?, board = ?, current_version = ?, "
                "current_payload_version = ?, slot = ?, representation = ?, fallback_reason = ?, "
                "confirmed = ?, last_offered_release_id = COALESCE(?, last_offered_release_id), "
                "registrar_ref = COALESCE(?, registrar_ref), account_id = ?, "
                "streams = COALESCE(?, streams), last_seen = ? WHERE device_id = ?",
                (product_id, board, current_version, current_payload_version, slot, representation,
                 fallback_reason, confirmed, last_offered_release_id, registrar_ref, account_id,
                 ",".join(streams) if streams else None, now, device_id))

    def get_device(self, device_id: str) -> dict | None:
        return _d(self.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,)))

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

    def list_devices(self, product_id: int | None = None, limit: int = 100, account_id=None,
                     cohort=None, offset: int = 0) -> list[dict]:
        where, params = _scope(account_id, product_id)
        if cohort is not None:
            where = (where + " AND cohort = ?") if where else "WHERE cohort = ?"
            params = (*params, cohort)
        rows = self.query_all("SELECT * FROM devices " + where
                              + " ORDER BY last_seen DESC LIMIT ? OFFSET ?", (*params, limit, offset))
        return [_d(r) for r in rows]

    def fleet_summary(self, product_id: int | None = None, account_id=None) -> dict:
        where, params = _scope(account_id, product_id)
        by_version = {r["current_version"]: r["n"] for r in self.query_all(
            "SELECT current_version, COUNT(*) AS n FROM devices " + where
            + " GROUP BY current_version", params)}
        by_slot = {r["slot"]: r["n"] for r in self.query_all(
            "SELECT slot, COUNT(*) AS n FROM devices " + where + " GROUP BY slot", params)}
        total = self.query_one("SELECT COUNT(*) AS n FROM devices " + where, params)["n"]
        return {"total": total, "by_version": by_version, "by_slot": by_slot}

    def list_cohorts(self, product_id: int | None = None, account_id=None) -> list[dict]:
        """The cohorts in use (per board), with a device count each."""
        where, params = _scope(account_id, product_id)
        rows = self.query_all("SELECT cohort, COUNT(*) AS devices FROM devices " + where
                              + " GROUP BY cohort ORDER BY cohort", params)
        return [{"cohort": r["cohort"], "devices": r["devices"]} for r in rows]

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
        return self.execute(sql, tuple(params)).rowcount

    # --- version pins (device / cohort, override rollouts) ----------------------------------

    def set_device_pin(self, device_id: str, release_id: str | None) -> None:
        """Pin (or, with None, unpin) a device to a release. Preserved across check-ins."""
        self.execute("UPDATE devices SET pinned_release_id = ? WHERE device_id = ?",
                     (release_id, device_id))

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

    def deployment_counts(self, release_id: str) -> dict:
        """Reported {installed, failed} counts for a release (from explicit /feedback)."""
        rows = self.query_all(
            "SELECT status, COUNT(*) AS n FROM deployments WHERE release_id = ? GROUP BY status",
            (release_id,))
        by = {r["status"]: r["n"] for r in rows}
        return {"installed": by.get("installed", 0), "failed": by.get("failed", 0)}

    # --- accounts (tenants) -----------------------------------------------------------------

    def add_account(self, account_id: str, name: str) -> None:
        self.execute("INSERT INTO accounts (account_id, name, created_at) VALUES (?,?,?)",
                     (account_id, name, _now_iso()))

    def get_account(self, account_id: str) -> dict | None:
        return _d(self.query_one("SELECT * FROM accounts WHERE account_id = ?", (account_id,)))

    def list_accounts(self) -> list[dict]:
        return [_d(r) for r in self.query_all("SELECT * FROM accounts ORDER BY created_at")]

    def account_name_exists(self, name: str, except_id: str | None = None) -> bool:
        """Whether another account already uses ``name`` (case-insensitive). ``except_id`` excludes
        one account (so a rename to the same name is fine)."""
        return self.query_one(
            "SELECT 1 FROM accounts WHERE LOWER(name) = LOWER(?) AND account_id <> ?",
            (name, except_id or "")) is not None

    def rename_account(self, account_id: str, name: str) -> None:
        self.execute("UPDATE accounts SET name = ? WHERE account_id = ?", (name, account_id))

    def set_account_active(self, account_id: str, active: bool) -> None:
        self.execute("UPDATE accounts SET active = ? WHERE account_id = ?",
                     (1 if active else 0, account_id))

    # --- admin tokens (stored hashed) -------------------------------------------------------

    def add_token(self, token_hash: str, name: str, scopes: list[str], account_id: str = "") -> None:
        self.execute("INSERT INTO admin_tokens (token_hash, name, scopes, created_at, account_id) "
                     "VALUES (?,?,?,?,?)", (token_hash, name, ",".join(scopes), _now_iso(), account_id))

    def get_token(self, token_hash: str) -> dict | None:
        r = _d(self.query_one("SELECT * FROM admin_tokens WHERE token_hash = ?", (token_hash,)))
        if r is not None:
            r["scopes"] = r["scopes"].split(",") if r["scopes"] else []
        return r

    def revoke_token(self, token_hash: str) -> None:
        self.execute("UPDATE admin_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))

    def list_tokens(self, account_id=None) -> list[dict]:
        where, params = ("WHERE account_id = ?", (account_id,)) if account_id is not None else ("", ())
        rows = [_d(r) for r in self.query_all(
            "SELECT token_hash, name, scopes, account_id, created_at, revoked FROM admin_tokens "
            + where + " ORDER BY created_at", params)]
        for r in rows:
            r["scopes"] = r["scopes"].split(",") if r["scopes"] else []
        return rows

    def revoke_account_tokens(self, account_id: str) -> int:
        """Revoke every live token for an account (the token half of deactivation). Returns count."""
        return self.execute("UPDATE admin_tokens SET revoked = 1 WHERE account_id = ? AND revoked = 0",
                            (account_id,)).rowcount

    def count_tokens(self) -> int:
        return self.query_one("SELECT COUNT(*) AS n FROM admin_tokens")["n"]

    # --- the hash-chained audit log ---------------------------------------------------------

    def append_audit(self, *, actor, action, entity_type=None, entity_id=None, data=None,
                     account_id="") -> int:
        last = self.query_one("SELECT seq, entry_hash FROM audit ORDER BY seq DESC LIMIT 1")
        seq = (last["seq"] + 1) if last else 1
        prev = last["entry_hash"] if last else ""
        ts = _now_iso()
        payload = json.dumps(data or {}, separators=(",", ":"), sort_keys=True)
        entry = _audit_hash(prev, ts, actor, action, entity_type, entity_id, payload)
        self.execute(
            "INSERT INTO audit (seq, ts, actor, action, entity_type, entity_id, data, prev_hash, "
            "entry_hash, account_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (seq, ts, actor, action, entity_type, entity_id, payload, prev, entry, account_id))
        return seq

    def read_audit(self, limit: int = 100, since_seq: int = 0, account_id=None) -> list[dict]:
        sql = "SELECT * FROM audit WHERE seq > ?"
        params = [since_seq]
        if account_id is not None:
            sql += " AND account_id = ?"
            params.append(account_id)
        rows = [_d(r) for r in self.query_all(sql + " ORDER BY seq LIMIT ?", (*params, limit))]
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

    def __init__(self, dsn: str, connect=None):
        super().__init__((connect or self._default_connect(dsn))())

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
