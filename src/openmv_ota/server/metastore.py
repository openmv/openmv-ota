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

import threading

from .errors import ServerError

# Each entry is a list of DDL statements; its 1-based index is the schema version it defines.
# (Feature tables are appended here by the steps that introduce them.)
_MIGRATIONS: list[list[str]] = []


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
