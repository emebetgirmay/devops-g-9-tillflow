"""SQLite storage, constraints and the shared helpers (ADR 0006 consequences, ADR 0002 later).

Uniqueness that protects money lives in the schema, not only in code: the idempotency key primary
key, the unique provider references and receipts, the unique ledger entry per provider reference,
the unique outbox event per record, and the partial unique indexes that allow one live payment per
sale and one live disbursement per payout. Postgres arrives with RDS; until then this is sqlite.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any

from core import db, jsonlog, metrics, otlp, tracing
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
  updated_at REAL NOT NULL,
  trace_id TEXT,
  span_id TEXT
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
  updated_at REAL NOT NULL,
  trace_id TEXT,
  span_id TEXT
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
    def __init__(self, target: str, pool_size: int = 5) -> None:
        """`target` is a SQLite file path or a postgresql:// URL (core/db.py)."""
        self.db = db.open_database(target, pool_size)
        self.db.create_schema(SCHEMA)
        # A database created before these columns existed: CREATE TABLE IF NOT EXISTS skips it.
        for table in ("payments", "disbursements"):
            for column in ("trace_id", "span_id"):
                self.db.ensure_column(table, column)

    def connection(self):
        """Autocommit connection for reads and single statements."""
        return self.db.connection()

    def tx(self):
        """One write transaction, one writer at a time: a payment or callback is read and updated
        with no other writer in between (BEGIN IMMEDIATE on SQLite, an advisory lock on
        PostgreSQL; see core/db.py)."""
        return self.db.tx()

    def close(self) -> None:
        self.db.close()

    def ping(self) -> bool:
        try:
            with self.connection() as conn:
                conn.execute("SELECT 1").fetchone()
        except db.Error:
            return False
        return True

    # Idempotency ---------------------------------------------------------------------------

    @staticmethod
    def idem_lookup(
        conn: db.Connection, tenant_id: str, operation: str, key: str
    ) -> db.Row | None:
        return conn.execute(
            "SELECT * FROM idempotency_keys WHERE tenant_id = ? AND operation = ? AND idem_key = ?",
            (tenant_id, operation, key),
        ).fetchone()

    @staticmethod
    def idem_classify(row: db.Row | None, fingerprint: str) -> str:
        """'new', 'mismatch', 'replay' or 'in_flight'."""
        if row is None:
            return "new"
        if row["request_fingerprint"] != fingerprint:
            return "mismatch"
        return "replay" if row["status"] == "COMPLETED" else "in_flight"

    @staticmethod
    def idem_begin(
        conn: db.Connection,
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
        conn: db.Connection,
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
        conn: db.Connection,
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
        metrics.state_transitions_total.inc(kind, current.value, target.value)
        # The record remembers the trace and span that created it, so a callback, sweep or
        # reconcile that moves it later (under its own trace id) still belongs to the sale's trace:
        # in the logs as origin_trace_id, and in X-Ray as a child span of the creating request.
        trace_id = tracing.current_trace_id()
        creator = conn.execute(
            f"SELECT trace_id, span_id FROM {table} WHERE {id_column} = ?", (record_id,)
        ).fetchone()
        origin = creator["trace_id"]
        if origin and origin != trace_id:
            moved_at = time.time()  # wall clock: `now` is the provider clock, which tests advance
            otlp.export(
                trace_id=origin,
                span_id=tracing.new_span_id(),
                parent_span_id=creator["span_id"],
                # "to", not "->": X-Ray strips ">" from span names.
                name=f"{kind} {current.value} to {target.value}",
                start=moved_at - 0.001,
                end=moved_at,
                kind=otlp.INTERNAL,
                attributes={"tillflow.record_id": record_id, "tillflow.request_trace_id": trace_id},
            )
        jsonlog.log_line(
            level="INFO",
            service="payments",
            event="state_transition",
            trace_id=trace_id,
            record_kind=kind,
            record_id=record_id,
            state=target.value,
            origin_trace_id=origin if origin != trace_id else None,
        )

    @staticmethod
    def ledger(
        conn: db.Connection,
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
            "INSERT INTO ledger_entries (tenant_id, record_id, entry_type, amount_minor,"
            " currency, provider, provider_ref, receipt, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT DO NOTHING",
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
        conn: db.Connection,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        now: float,
    ) -> bool:
        cursor = conn.execute(
            "INSERT INTO outbox (aggregate_id, event_type, payload, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT DO NOTHING",
            (aggregate_id, event_type, json.dumps(payload, sort_keys=True), now),
        )
        return cursor.rowcount == 1

    @staticmethod
    def anomaly(
        conn: db.Connection,
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
            # Ties the anomaly to the request that caused it (a replayed or reordered callback).
            "trace_id": tracing.current_trace_id(),
        }
        print(json.dumps(line, sort_keys=True), file=sys.stderr, flush=True)
        metrics.anomalies_total.inc(kind, severity)

    @staticmethod
    def get_flag(conn: db.Connection, name: str, default: bool) -> bool:
        row = conn.execute("SELECT value FROM flags WHERE name = ?", (name,)).fetchone()
        return default if row is None else bool(row["value"])

    @staticmethod
    def set_flag(conn: db.Connection, name: str, value: bool, reason: str, now: float) -> None:
        conn.execute(
            "INSERT INTO flags (name, value, reason, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (name) DO UPDATE SET value = excluded.value, reason = excluded.reason,"
            " updated_at = excluded.updated_at",
            (name, int(value), reason, now),
        )
