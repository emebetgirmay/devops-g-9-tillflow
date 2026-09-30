"""Where Commission keeps its ledger: SQLite for local runs and unit tests, PostgreSQL when
deployed (ADR 0002). The same layer as services/payments/core/db.py (the two images share no
code), so the SQL in store.py, close.py and disburse.py is written once, in the portable subset:
`?` placeholders, ON CONFLICT, named columns.

Both backends give the same two things:

- ``connection()``: autocommit, for reads and single statements.
- ``tx()``: one write transaction, and **only one at a time across the whole service**. SQLite
  does that with BEGIN IMMEDIATE. PostgreSQL does it with a transaction-scoped advisory lock taken
  first, so an attendant's close or a payout's update is still read and written with no other
  writer in between, even if two scheduled runs overlap.

  ponytail: a single writer lock. Commission is a batch job, so this costs nothing today; move
  to per-tenant locks if several tenants' closes ever need to run in parallel.

psycopg is imported only when a PostgreSQL URL is given, so SQLite runs need nothing installed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

# What callers catch, whichever backend raised it. The PostgreSQL classes are added when that
# backend loads, so read these as ``db.IntegrityError`` (an attribute), never ``from db import``.
IntegrityError: tuple[type[Exception], ...] = (sqlite3.IntegrityError,)
Error: tuple[type[Exception], ...] = (sqlite3.Error,)

Connection = Any  # sqlite3.Connection or _PgConnection: .execute(sql, params) -> cursor
Row = Any  # sqlite3.Row or dict: row["column"]

# Any 64-bit constant, shared by every Commission run so they queue on the same lock, and
# different from Payments' so the two services never wait on each other.
WRITER_LOCK = 0x636F6D6D  # "comm"


def is_postgres(target: str) -> bool:
    return target.startswith(("postgresql://", "postgres://"))


def open_database(target: str, pool_size: int = 5):
    return _Postgres(target, pool_size) if is_postgres(target) else _Sqlite(target)


class _Sqlite:
    dialect = "sqlite"

    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def create_schema(self, ddl: str) -> None:
        with self.connection() as conn:
            conn.executescript(ddl)

    def ensure_column(self, table: str, column: str) -> None:
        """For a database file created before the column existed."""
        with self.connection() as conn:
            if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    def close(self) -> None:
        pass


@lru_cache(maxsize=512)
def _pg_sql(sql: str) -> str:
    # The SQL here has no literal `?` or `%`, so the placeholder swap is a plain replace.
    return sql.replace("?", "%s")


class _PgConnection:
    """psycopg connection with sqlite3's ``execute(sql, params)`` shape and ``?`` placeholders."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def execute(self, sql: str, params: Any = ()) -> Any:
        return self._raw.execute(_pg_sql(sql), tuple(params) or None)


class _Postgres:
    dialect = "postgres"

    def __init__(self, url: str, pool_size: int) -> None:
        global IntegrityError, Error
        import psycopg
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        IntegrityError = (sqlite3.IntegrityError, psycopg.IntegrityError)
        Error = (sqlite3.Error, psycopg.Error)
        # A pool, because a new TLS connection per request costs more than the request. Rows come
        # back as dicts so row["column"] works as it does with sqlite3.Row.
        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=max(pool_size, 1),
            timeout=30,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        with self._pool.connection() as raw:
            yield _PgConnection(raw)

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        with self._pool.connection() as raw, raw.transaction():
            # Give up after 30 s (sqlite's busy_timeout) instead of queueing forever behind a
            # stuck writer; the lock itself is released at commit or rollback.
            raw.execute("SET LOCAL lock_timeout = '30s'")
            raw.execute("SELECT pg_advisory_xact_lock(%s)", (WRITER_LOCK,))
            yield _PgConnection(raw)

    def create_schema(self, ddl: str) -> None:
        ddl = ddl.replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
        ).replace(" REAL", " DOUBLE PRECISION")  # REAL is 4 bytes here: too coarse for epoch seconds
        with self.tx() as conn:  # under the lock: two tasks starting together must not race the DDL
            conn.execute(ddl)

    def ensure_column(self, table: str, column: str) -> None:
        with self.tx() as conn:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} TEXT")

    def close(self) -> None:
        self._pool.close()
