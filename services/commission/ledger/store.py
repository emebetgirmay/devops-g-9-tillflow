"""SQLite storage for the commission ledger (ADR 0008 section 2).

Constraints that protect money live in the schema:
- one non-failed payout_ledger row per (tenant, attendant, business_date);
- payout_items.sale_id is unique, so a sale is linked to a payout at most once even if the close
  job is replayed or run concurrently;
- carry_forward and pos_cursors hold exactly one row per (tenant, attendant) / tenant, updated in
  the same transaction as the ledger and item writes so a crash mid-close cannot lose or double
  count a sale's commission.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS payout_ledger (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  attendant_id TEXT NOT NULL,
  business_date TEXT NOT NULL,
  currency TEXT NOT NULL,
  amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
  msisdn TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('PLANNED', 'REQUESTED', 'SUCCEEDED', 'FAILED')),
  idempotency_key TEXT NOT NULL,
  disbursement_id TEXT,
  failure_reason TEXT,
  reconcile_attempts INTEGER NOT NULL DEFAULT 0,
  next_reconcile_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS payout_ledger_one_live_per_day
  ON payout_ledger (tenant_id, attendant_id, business_date)
  WHERE state <> 'FAILED';

CREATE TABLE IF NOT EXISTS payout_items (
  id TEXT PRIMARY KEY,
  sale_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL,
  attendant_id TEXT NOT NULL,
  business_date TEXT NOT NULL,
  sale_total_minor INTEGER NOT NULL,
  commission_rate_bps INTEGER NOT NULL,
  commission_minor INTEGER NOT NULL,
  paid_at TEXT NOT NULL,
  payout_ledger_id TEXT REFERENCES payout_ledger (id),
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS payout_items_unlinked
  ON payout_items (tenant_id, attendant_id)
  WHERE payout_ledger_id IS NULL;

CREATE TABLE IF NOT EXISTS carry_forward (
  tenant_id TEXT NOT NULL,
  attendant_id TEXT NOT NULL,
  amount_minor INTEGER NOT NULL DEFAULT 0 CHECK (amount_minor >= 0),
  updated_at REAL NOT NULL,
  PRIMARY KEY (tenant_id, attendant_id)
);

CREATE TABLE IF NOT EXISTS pos_cursors (
  tenant_id TEXT PRIMARY KEY,
  last_paid_at TEXT,
  last_sale_id TEXT,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS close_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  business_date TEXT NOT NULL,
  sales_seen INTEGER NOT NULL,
  attendants_closed INTEGER NOT NULL,
  ran_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS anomalies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  severity TEXT NOT NULL,
  tenant_id TEXT,
  attendant_id TEXT,
  payout_ledger_id TEXT,
  detail TEXT,
  created_at REAL NOT NULL
);
"""


class StaleStateError(Exception):
    """The row changed under us: another writer already moved this record."""


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. BEGIN IMMEDIATE serialises writers over one attendant's close
        or disbursement update, matching services/payments/core/store.py::Store.tx."""
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

    def ping(self) -> bool:
        try:
            with self.connection() as conn:
                conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    @staticmethod
    def anomaly(
        conn: sqlite3.Connection,
        *,
        kind: str,
        severity: str,
        now: float,
        tenant_id: str | None = None,
        attendant_id: str | None = None,
        payout_ledger_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        import json
        import sys

        conn.execute(
            "INSERT INTO anomalies (kind, severity, tenant_id, attendant_id, payout_ledger_id,"
            " detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, severity, tenant_id, attendant_id, payout_ledger_id, detail, now),
        )
        print(
            json.dumps(
                {
                    "level": "ERROR" if severity in ("critical", "high") else "WARN",
                    "event": "anomaly",
                    "kind": kind,
                    "severity": severity,
                    "tenant_id": tenant_id,
                    "attendant_id": attendant_id,
                    "detail": detail,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
