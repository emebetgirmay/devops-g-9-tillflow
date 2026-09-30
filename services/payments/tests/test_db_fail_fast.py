"""G4 finding 6: while the database is away, Payments must fail fast, not queue for 30 s.

The pool is built with a short wait, a connect timeout and a check before a connection is handed
out. Asserted on the arguments, so no database is needed; the live re-run is in
evidence/payments-integrity/postgres/fail-fast.json.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

import _bootstrap  # noqa: F401

from core import db


class FailFastPoolTest(unittest.TestCase):
    def test_pool_waits_briefly_connects_briefly_and_checks_connections(self) -> None:
        built: dict = {}

        class FakePool:
            check_connection = staticmethod(lambda conn: None)

            def __init__(self, url, **kwargs):
                built.update(kwargs, url=url)

        fake_pool_module = types.SimpleNamespace(ConnectionPool=FakePool)
        fake_rows = types.SimpleNamespace(dict_row=object())
        fake_psycopg = types.SimpleNamespace(IntegrityError=type("E", (Exception,), {}),
                                             Error=type("E2", (Exception,), {}), rows=fake_rows)
        modules = {"psycopg": fake_psycopg, "psycopg.rows": fake_rows, "psycopg_pool": fake_pool_module}
        saved = (db.IntegrityError, db.Error)
        try:
            with mock.patch.dict(sys.modules, modules):
                db._Postgres("postgresql://u@h/db", pool_size=5)
        finally:
            db.IntegrityError, db.Error = saved
        self.assertLessEqual(built["timeout"], 5)
        self.assertLessEqual(built["kwargs"]["connect_timeout"], 5)
        self.assertIs(built["check"], FakePool.check_connection)


if __name__ == "__main__":
    unittest.main()
