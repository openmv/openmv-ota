"""The SQL metadata store: migrations, the meta kv, sqlite/postgres dispatch + param style."""

from __future__ import annotations

import sys

import pytest

from openmv_ota.server import metastore as ms
from openmv_ota.server.errors import ServerError
from openmv_ota.server.metastore import (
    PostgresMetadataStore,
    SqliteMetadataStore,
    _sqlite_path,
    build_metastore,
)
from openmv_ota.server.settings import ServerSettings


def _mem() -> SqliteMetadataStore:
    return SqliteMetadataStore(":memory:")


def _settings(**kw):
    kw.setdefault("swd_ids_verify_url", "u")
    kw.setdefault("swd_ids_verify_token", "t")
    return ServerSettings(**kw)


def test_migrate_creates_meta_and_records_version():
    s = _mem()
    v = s.migrate()
    assert v == len(ms._MIGRATIONS)
    assert s.get_meta("schema_version") == str(v)
    assert s.migrate() == v                         # idempotent


def test_meta_upsert():
    s = _mem()
    s.migrate()
    assert s.get_meta("k") is None
    s.set_meta("k", "v1")
    assert s.get_meta("k") == "v1"
    s.set_meta("k", "v2")
    assert s.get_meta("k") == "v2"


def test_migrate_applies_pending_migrations(monkeypatch):
    monkeypatch.setattr(ms, "_MIGRATIONS", [["CREATE TABLE t1 (id INTEGER)"]])
    s = _mem()
    assert s.migrate() == 1
    s.execute("INSERT INTO t1 (id) VALUES (?)", (5,))
    assert s.query_one("SELECT id FROM t1")["id"] == 5
    assert s.query_all("SELECT id FROM t1")[0]["id"] == 5
    assert s.migrate() == 1                          # re-run doesn't re-apply (no CREATE error)


def test_param_style_translation():
    assert _mem()._sql("SELECT ? , ?") == "SELECT ? , ?"          # sqlite: unchanged
    pg = PostgresMetadataStore("postgresql://x", connect=lambda: _mem()._conn)
    assert pg.paramstyle == "%s" and pg._sql("SELECT ? , ?") == "SELECT %s , %s"


def test_postgres_missing_psycopg_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ServerError, match="server-postgres"):
        PostgresMetadataStore("postgresql://x")


def test_sqlite_path_parsing():
    assert _sqlite_path("sqlite:///:memory:") == ":memory:"
    assert _sqlite_path("sqlite:///./ota.db") == "./ota.db"
    assert _sqlite_path("sqlite:////abs/ota.db") == "/abs/ota.db"


def test_build_metastore_sqlite(tmp_path):
    s = build_metastore(_settings(database_url="sqlite:///" + str(tmp_path / "ota.db")))
    assert isinstance(s, SqliteMetadataStore)
    s.migrate()
    s.close()


def test_build_metastore_postgres_dispatches(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ServerError, match="server-postgres"):
        build_metastore(_settings(database_url="postgresql://x"))


def test_build_metastore_unsupported_url():
    with pytest.raises(ServerError, match="unsupported database_url"):
        build_metastore(_settings(database_url="mysql://x"))


class _Recorder:
    """A DBAPI-shaped connection that records every statement and commit, answering the
    Postgres-only lock-hygiene query with a canned count."""

    def __init__(self, idle=0):
        self.sql, self.commits, self.idle = [], 0, idle

    def cursor(self):
        rec = self

        class Cur:
            rowcount = 0

            def execute(self, sql, params=()):
                rec.sql.append(sql)

            def fetchone(self):
                if "pg_stat_activity" in rec.sql[-1]:
                    return {"n": rec.idle}
                return {"value": None} if "FROM meta" in rec.sql[-1] else None

            def fetchall(self):
                return []
        return Cur()

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def test_reads_end_their_transaction():
    """A SELECT must not leave the connection idle-in-transaction (on Postgres that holds
    a share lock forever and blocks the next deploy's schema change)."""
    conn = _Recorder()
    s = PostgresMetadataStore("postgresql://x", connect=lambda: conn)
    s.query_one("SELECT 1")
    s.query_all("SELECT 1")
    assert conn.commits == 2


def test_postgres_migrate_clears_blockers_and_caps_the_lock_wait(monkeypatch, capsys):
    monkeypatch.setattr(ms, "_MIGRATIONS", [["CREATE TABLE t1 (id INTEGER)"]])
    conn = _Recorder(idle=2)
    s = PostgresMetadataStore("postgresql://x", connect=lambda: conn)
    assert s.migrate() == 1
    joined = "\n".join(conn.sql)
    i_kill, i_cap, i_ddl, i_reset = (joined.index("pg_terminate_backend"),
                                     joined.index("SET lock_timeout = '30s'"),
                                     joined.index("CREATE TABLE t1"),
                                     joined.index("RESET lock_timeout"))
    assert i_kill < i_cap < i_ddl < i_reset                  # hygiene, cap, DDL, restore
    assert "idle in transaction" in joined and "pid <> pg_backend_pid()" in joined
    assert "ended 2 idle-in-transaction" in capsys.readouterr().err
    # nothing pending: no hygiene, no lock games -- token ops call migrate() routinely
    conn2 = _Recorder(idle=5)
    s2 = PostgresMetadataStore("postgresql://x", connect=lambda: conn2)
    monkeypatch.setattr(ms, "_MIGRATIONS", [])
    s2.migrate()
    assert not any("pg_terminate_backend" in q or "lock_timeout" in q for q in conn2.sql)
    assert "ended" not in capsys.readouterr().err


def test_migrations_are_append_only_and_v23_rekeys_a_real_database(tmp_path):
    """Two things this pins, both learned the hard way.

    **Order is identity.** A migration's version IS its position in the list, and a
    deployed server records the last one it ran. Inserting a new migration ABOVE an old
    one renumbers history: the server's next deploy skips the new work and re-applies
    something it already did -- here, adding a column twice, which fails the migration
    and takes the boot down with it. New migrations append.

    **Dependants are rewritten before the thing they point at.** v23 board-qualifies
    device ids; the binding and the install history join on the OLD key, so they move
    first. Rekeying devices first would strand both against an id that no longer exists.
    """
    from openmv_ota.server import metastore as M

    assert "pause_reason" in M._MIGRATIONS[19][0]          # v20, as production recorded it
    assert "product_id TYPE BIGINT" in M._MIGRATIONS[20][0]
    assert "admin_tokens ADD COLUMN products" in M._MIGRATIONS[21][0]
    assert "device_accounts" in M._MIGRATIONS[22][0]       # dependants first
    assert "UPDATE devices SET device_id" in M._MIGRATIONS[22][-1]

    db = str(tmp_path / "prod.db")
    full = M._MIGRATIONS
    try:                                                   # a server last deployed at v20
        M._MIGRATIONS = full[:20]
        old = M.SqliteMetadataStore(db)
        assert old.migrate() == 20
        old.upsert_device(device_id="3c0021000c51", product_id=7, board="OPENMV_N6",
                          account_id="acct")
        old.bind_device_account("3c0021000c51", "acct", source="learned")
        old.record_deployment(device_id="3c0021000c51", release_id="rel_1", product_id=7,
                              status="installed", reason=None)
        old.upsert_device(device_id="noboard", product_id=7, board=None, account_id="acct")
    finally:
        M._MIGRATIONS = full

    store = M.SqliteMetadataStore(db)
    assert store.migrate() == 23                           # the deploy applies 21, 22, 23
    assert sorted(d["device_id"] for d in store.list_devices()) == [
        "OPENMV_N6:3c0021000c51", "noboard"]               # board-less rows are left alone
    assert store.device_account("OPENMV_N6:3c0021000c51")["account_id"] == "acct"
    assert store.query_all("SELECT device_id FROM deployments")[0]["device_id"] == \
        "OPENMV_N6:3c0021000c51"
    store.add_token("h", "t", ["observe"], account_id="acct", products=[7])
    assert store.get_token("h")["products"] == [7]
    assert M.SqliteMetadataStore(db).migrate() == 23        # idempotent
