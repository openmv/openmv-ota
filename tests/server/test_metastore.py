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
