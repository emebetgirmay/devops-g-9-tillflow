"""Runs the real Payments app in-process (FakeAdapter, manual clock) for the worker tests."""

from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

import _bootstrap  # noqa: F401
from app import App, make_handler
from core.config import Settings
from mpesa import FakeAdapter, ManualClock


class PaymentsServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        settings = replace(Settings(), db_path=str(Path(self._dir.name) / "payments.db"))
        self.clock = ManualClock(1_000_000.0)
        self.adapter = FakeAdapter(clock=self.clock)
        self.app = App(settings, clock=self.clock, adapter=self.adapter)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def rows(self, sql: str) -> list:
        with self.app.store.connection() as conn:
            return conn.execute(sql).fetchall()

    def count(self, table: str, where: str = "1=1") -> int:
        return self.rows(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")[0]["n"]

    def deliver_results(self) -> None:
        self.clock.advance(2)
        self.app.dispatch("POST", "/_fake/deliver-callbacks", {}, b"{}", "127.0.0.1")
