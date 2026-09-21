"""SQLite storage, constraints and the shared helpers (ADR 0006 consequences, ADR 0002 later).

Uniqueness that protects money lives in the schema, not only in code: the idempotency key primary
key, the unique provider references and receipts, the unique ledger entry per provider reference,
the unique outbox event per record, and the partial unique indexes that allow one live payment per
sale and one live disbursement per payout. Postgres arrives with RDS; until then this is sqlite.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any

from core.states import check_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
  tenant_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  idem_key TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('IN_FLIGHT', 'COMPLETED')),
  response_code INTEGER,
  response_body TEXT,
  resource_id TEXT,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  PRIMARY KEY (tenant_id, operation, idem_key)
);

CREATE TABLE IF NOT EXISTS payments (
  payment_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  idem_key TEXT NOT NULL,
  sale_id TEXT,
  msisdn TEXT NOT NULL,
  amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
  currency TEXT NOT NULL,
  reference TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('CREATED', 'PENDING', 'UNKNOWN', 'NEEDS_REVIEW', 'SUCCEEDED', 'DECLINED', 'EXPIRED')),
  provider_ref TEXT UNIQUE,
  merchant_request_id TEXT,
  decline_reason TEXT,
  receipt TEXT UNIQUE,
  raw_code TEXT,
  initiate_started_at REAL,
  pending_since REAL,
  unknown_since REAL,
  reconcile_attempts INTEGER NOT NULL DEFAULT 0,
  next_reconcile_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS payments_one_live_per_sale
  ON payments (tenant_id, sale_id)
  WHERE sale_id IS NOT NULL AND state NOT IN ('DECLINED', 'EXPIRED');

CREATE TABLE IF NOT EXISTS disbursements (
  disbursement_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  idem_key TEXT NOT NULL,
  attendant_id TEXT NOT NULL,
  payout_period TEXT NOT NULL,
  payout_key TEXT NOT NULL,
  msisdn TEXT NOT NULL,
  amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
  currency TEXT NOT NULL,
  originator_conversation_id TEXT NOT NULL UNIQUE,
  conversation_id TEXT,
  state TEXT NOT NULL CHECK (state IN
    ('CREATED', 'PENDING', 'UNKNOWN', 'NEEDS_REVIEW', 'SUCCEEDED', 'FAILED')),
  failure_reason TEXT,
  receipt TEXT UNIQUE,
  raw_code TEXT,
  initiate_started_at REAL,
  pending_since REAL,
  unknown_since REAL,
  reconcile_attempts INTEGER NOT NULL DEFAULT 0,
  next_reconcile_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS disbursements_one_live_per_payout
  ON disbursements (payout_key)
  WHERE state <> 'FAILED';

CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  entry_type TEXT NOT NULL CHECK (entry_type IN ('PAYMENT_CREDIT', 'DISBURSEMENT_DEBIT')),
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_ref TEXT NOT NULL,
  receipt TEXT,
  created_at REAL NOT NULL,
  UNIQUE (provider, provider_ref, entry_type)
);

CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  aggregate_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE (aggregate_id, event_type)
);

-- Callback audit keeps a hash and the parsed summary, not the body: real bodies carry names,
-- phone numbers and balances that must stay out of storage (ADR 0008).
CREATE TABLE IF NOT EXISTS provider_callbacks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  provider_ref TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  outcome TEXT NOT NULL,
  raw_code TEXT NOT NULL,
  received_at REAL NOT NULL,
  UNIQUE (provider_ref, payload_sha256)
);

CREATE TABLE IF NOT EXISTS unmatched_callbacks (
  kind TEXT NOT NULL,
  provider_ref TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  outcome TEXT NOT NULL,
  raw_code TEXT NOT NULL,
  amount_minor INTEGER,
  received_at REAL NOT NULL,
  PRIMARY KEY (provider_ref, payload_sha256)
);

CREATE TABLE IF NOT EXISTS anomalies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  severity TEXT NOT NULL,
  record_type TEXT,
  record_id TEXT,
  provider_ref TEXT,
  from_state TEXT,
  event TEXT,
  detail TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS flags (
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL,
  reason TEXT,
  updated_at REAL NOT NULL
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
        """One write transaction. BEGIN IMMEDIATE serialises writers, so a payment or callback is
        read and updated under the lock (the sqlite equivalent of SELECT ... FOR UPDATE)."""
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

    # Idempotency ---------------------------------------------------------------------------

    @staticmethod
    def idem_lookup(
        conn: sqlite3.Connection, tenant_id: str, operation: str, key: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM idempotency_keys WHERE tenant_id = ? AND operation = ? AND idem_key = ?",
            (tenant_id, operation, key),
        ).fetchone()

    @staticmethod
    def idem_classify(row: sqlite3.Row | None, fingerprint: str) -> str:
        """'new', 'mismatch', 'replay' or 'in_flight'."""
        if row is None:
            return "new"
        if row["request_fingerprint"] != fingerprint:
            return "mismatch"
        return "replay" if row["status"] == "COMPLETED" else "in_flight"

    @staticmethod
    def idem_begin(
        conn: sqlite3.Connection,
        tenant_id: str,
        operation: str,
        key: str,
        fingerprint: str,
        now: float,
        ttl: float,
    ) -> None:
        conn.execute(
            "INSERT INTO idempotency_keys (tenant_id, operation, idem_key, request_fingerprint,"
            " status, created_at, expires_at) VALUES (?, ?, ?, ?, 'IN_FLIGHT', ?, ?)",
            (tenant_id, operation, key, fingerprint, now, now + ttl),
        )

    @staticmethod
    def idem_complete(
        conn: sqlite3.Connection,
        tenant_id: str,
        operation: str,
        key: str,
        code: int,
        body: dict[str, Any],
        resource_id: str,
    ) -> None:
        conn.execute(
            "UPDATE idempotency_keys SET status = 'COMPLETED', response_code = ?,"
            " response_body = ?, resource_id = ? WHERE tenant_id = ? AND operation = ?"
            " AND idem_key = ?",
            (code, json.dumps(body, sort_keys=True), resource_id, tenant_id, operation, key),
        )

    # State, ledger, outbox, anomalies, flags -----------------------------------------------

    @staticmethod
    def transition(
        conn: sqlite3.Connection,
        *,
        kind: str,
        table: str,
        id_column: str,
        record_id: str,
        current: Enum,
        target: Enum,
        now: float,
        **fields: Any,
    ) -> None:
        """The only writer of a state column. Compare-and-swap on the current state.

        table, id_column and the field names come from this codebase, never from a request."""
        check_transition(kind, current, target)
        assignments = ["state = ?", "updated_at = ?", *(f"{name} = ?" for name in fields)]
        params = [target.value, now, *fields.values(), record_id, current.value]
        cursor = conn.execute(
            f"UPDATE {table} SET {', '.join(assignments)} WHERE {id_column} = ? AND state = ?",
            params,
        )
        if cursor.rowcount != 1:
            raise StaleStateError(f"{table} {record_id} is no longer {current.value}")

    @staticmethod
    def ledger(
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        record_id: str,
        entry_type: str,
        amount_minor: int,
        currency: str,
        provider: str,
        provider_ref: str,
        receipt: str | None,
        now: float,
    ) -> bool:
        """Insert the ledger entry; False if one already existed (a replay is a no-op)."""
        cursor = conn.execute(
            "INSERT OR IGNORE INTO ledger_entries (tenant_id, record_id, entry_type, amount_minor,"
            " currency, provider, provider_ref, receipt, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                record_id,
                entry_type,
                amount_minor,
                currency,
                provider,
                provider_ref,
                receipt,
                now,
            ),
        )
        return cursor.rowcount == 1

    @staticmethod
    def outbox(
        conn: sqlite3.Connection,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        now: float,
    ) -> bool:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO outbox (aggregate_id, event_type, payload, created_at)"
            " VALUES (?, ?, ?, ?)",
            (aggregate_id, event_type, json.dumps(payload, sort_keys=True), now),
        )
        return cursor.rowcount == 1

    @staticmethod
    def anomaly(
        conn: sqlite3.Connection,
        *,
        kind: str,
        severity: str,
        now: float,
        record_type: str | None = None,
        record_id: str | None = None,
        provider_ref: str | None = None,
        from_state: str | None = None,
        event: str | None = None,
        detail: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO anomalies (kind, severity, record_type, record_id, provider_ref,"
            " from_state, event, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, severity, record_type, record_id, provider_ref, from_state, event, detail, now),
        )
        line = {
            "level": "ERROR" if severity in ("critical", "high") else "WARN",
            "event": "anomaly",
            "kind": kind,
            "severity": severity,
            "record_type": record_type,
            "record_id": record_id,
            "detail": detail,
        }
        print(json.dumps(line, sort_keys=True), file=sys.stderr, flush=True)

    @staticmethod
    def get_flag(conn: sqlite3.Connection, name: str, default: bool) -> bool:
        row = conn.execute("SELECT value FROM flags WHERE name = ?", (name,)).fetchone()
        return default if row is None else bool(row["value"])

    @staticmethod
    def set_flag(conn: sqlite3.Connection, name: str, value: bool, reason: str, now: float) -> None:
        conn.execute(
            "INSERT INTO flags (name, value, reason, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (name) DO UPDATE SET value = excluded.value, reason = excluded.reason,"
            " updated_at = excluded.updated_at",
            (name, int(value), reason, now),
        )
