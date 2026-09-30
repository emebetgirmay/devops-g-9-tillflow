"""Path setup. The worker itself imports none of this; only the tests run the real Payments app."""

from __future__ import annotations

import sys
from pathlib import Path

COMMISSION_DIR = Path(__file__).resolve().parent.parent
SERVICES = COMMISSION_DIR.parent
for path in (COMMISSION_DIR, SERVICES / "payments", SERVICES / "_shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _use_postgres_for_tests(url: str) -> None:
    """TEST_POSTGRES_URL=postgresql://... runs this same suite on PostgreSQL: each SQLite file a
    test would have created becomes its own schema, for Commission's ledger and for the real
    Payments app these tests run in process. See services/payments/tests/_bootstrap.py."""
    import uuid

    import psycopg
    from core import db as payments_db

    from ledger import db as ledger_db

    schemas: dict[str, str] = {}
    opened: list = []

    def patch(module) -> None:
        real_open = module.open_database

        def open_database(target: str, pool_size: int = 5):
            if module.is_postgres(target):
                return real_open(target, pool_size)
            if target not in schemas:
                schemas[target] = "t_" + uuid.uuid4().hex[:20]
                with psycopg.connect(url, autocommit=True) as conn:
                    conn.execute(f"CREATE SCHEMA {schemas[target]}")
            joiner = "&" if "?" in url else "?"
            backend = real_open(f"{url}{joiner}options=-csearch_path%3D{schemas[target]}", 3)
            opened.append(backend)
            while len(opened) > 8:
                opened.pop(0).close()
            return backend

        module.open_database = open_database

    patch(payments_db)
    patch(ledger_db)


if __import__("os").environ.get("TEST_POSTGRES_URL"):
    _use_postgres_for_tests(__import__("os").environ["TEST_POSTGRES_URL"])
