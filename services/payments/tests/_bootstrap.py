"""Puts the service directory and services/_shared on sys.path for the tests."""

from __future__ import annotations

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
SHARED_DIR = SERVICE_DIR.parent / "_shared"
for path in (SERVICE_DIR, SHARED_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _use_postgres_for_tests(url: str) -> None:
    """TEST_POSTGRES_URL=postgresql://... runs this same suite on PostgreSQL.

    Every test builds its own SQLite file; here each distinct file path becomes its own schema, so
    tests stay isolated and no test has to know which backend it is on. CI runs the suite both
    ways. Only the most recent few pools stay open, so a whole run fits in max_connections.
    """
    import uuid

    import psycopg

    from core import db

    real_open, schemas, opened = db.open_database, {}, []

    def open_database(target: str, pool_size: int = 5):
        if db.is_postgres(target):
            return real_open(target, pool_size)
        if target not in schemas:
            schemas[target] = "t_" + uuid.uuid4().hex[:20]
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(f"CREATE SCHEMA {schemas[target]}")
        joiner = "&" if "?" in url else "?"
        backend = real_open(f"{url}{joiner}options=-csearch_path%3D{schemas[target]}", 3)
        opened.append(backend)
        while len(opened) > 6:
            opened.pop(0).close()
        return backend

    db.open_database = open_database


if __import__("os").environ.get("TEST_POSTGRES_URL"):
    _use_postgres_for_tests(__import__("os").environ["TEST_POSTGRES_URL"])
