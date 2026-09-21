"""Test fixtures: a fresh App on a temp database with a manual clock and the FakeAdapter."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import _bootstrap  # noqa: F401
from mpesa import FakeAdapter, ManualClock

from app import App
from core.config import Settings

KEY = "idem-key-0000000001"


def key(n: int) -> str:
    return f"idem-key-{n:010d}"


def payment_body(msisdn: str = "254000000001", amount: int = 150_000, **extra) -> dict:
    return {
        "tenant_id": "tenant-a",
        "msisdn": msisdn,
        "amount": amount,
        "account_reference": "ref1",
        **extra,
    }


def payout_body(msisdn: str = "254000000101", amount: int = 500_000, **extra) -> dict:
    return {
        "tenant_id": "tenant-a",
        "attendant_id": "att-1",
        "payout_period": "2026-09-20",
        "msisdn": msisdn,
        "amount": amount,
        **extra,
    }


class ServiceTestCase(unittest.TestCase):
    """Each test gets its own database. self.app.dispatch(...) drives the whole service."""

    settings_overrides: ClassVar[dict] = {}

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        settings = replace(
            Settings(), db_path=str(Path(self._dir.name) / "payments.db"), **self.settings_overrides
        )
        self.clock = ManualClock(1_000_000.0)
        self.adapter = FakeAdapter(clock=self.clock)
        self.app = App(settings, clock=self.clock, adapter=self.adapter)

    # Thin wrappers ---------------------------------------------------------------------------

    def call(self, method: str, path: str, body=None, headers=None, remote: str = "127.0.0.1"):
        raw = (
            b""
            if body is None
            else (body if isinstance(body, bytes) else json.dumps(body).encode())
        )
        return self.app.dispatch(method, path, headers or {}, raw, remote)

    def pay(self, idem: str = KEY, **body):
        return self.call("POST", "/payments", payment_body(**body), {"Idempotency-Key": idem})

    def payout(self, idem: str = KEY, **body):
        return self.call("POST", "/payouts", payout_body(**body), {"Idempotency-Key": idem})

    def deliver(self) -> list[dict]:
        return self.call("POST", "/_fake/deliver-callbacks", {}).body["delivered"]

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def payment(self, ident: str) -> dict:
        return self.call("GET", f"/payments/{ident}").body

    def disbursement(self, ident: str) -> dict:
        return self.call("GET", f"/payouts/{ident}").body

    def rows(self, sql: str, params=()) -> list:
        with self.app.store.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def count(self, table: str, where: str = "1=1", params=()) -> int:
        return self.rows(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)[0]["n"]
