"""Payouts: idempotent B2C disbursements for Commission (ADR 0008).

Commission calls this API only; it never touches the adapter or Daraja. Rules enforced here:
- payout_key is derived server-side from tenant, attendant and period, never taken from the caller;
- at most one disbursement per payout_key is not FAILED (partial unique index), so a stale key or a
  replayed run cannot pay twice;
- the provider identifier (OriginatorConversationID) is chosen by us, deterministically from the
  disbursement id, and stored before the provider call, so an unknown outcome can be reconciled;
- a timeout, a timeout notification, or a "duplicate originator id" answer is UNKNOWN, never FAILED,
  and nothing is ever resubmitted automatically;
- documented configuration failures and insufficient funds trip the payouts kill switch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import sys
import uuid
from typing import Any

from mpesa import (
    CallbackAuthenticityError,
    CallbackMalformedError,
    DisbursementRejectedError,
    DisbursementRequest,
    FailureReason,
    Outcome,
    OutcomeUnknownError,
    UnknownReferenceError,
)
from mpesa.fake_common import Clock
from mpesa.models import (
    IDEMPOTENCY_KEY_RE,
    MAX_DISBURSEMENT_MINOR,
    MIN_DISBURSEMENT_MINOR,
    MSISDN_RE,
)

from core.common import (
    PROVIDER,
    TEXT_RE,
    Reply,
    adapter_key,
    fingerprint,
    is_int,
    mask_msisdn,
    next_backoff,
    sha256_hex,
)
from core.config import Settings
from core.payments import ACK
from core.states import DisbursementState, IllegalTransition, is_terminal
from core.store import Store

OPERATION = "disbursement.create"
FLAG_PAYOUTS_ENABLED = "payouts_enabled"
TRIPPING_REASONS = frozenset({FailureReason.CONFIGURATION, FailureReason.INSUFFICIENT_FUNDS})
S = DisbursementState


def derive_payout_key(tenant_id: str, attendant_id: str, payout_period: str) -> str:
    """One payout per attendant per period. Derived here so callers cannot spoof de-duplication."""
    return "pk_" + sha256_hex(f"{tenant_id}|{attendant_id}|{payout_period}")[:40]


def originator_id_for(disbursement_id: str) -> str:
    """Deterministic 20-character provider id (the docs' sample is longer than their stated
    limit, so stay within 20 until the sandbox shows otherwise)."""
    digest = hashlib.sha256(disbursement_id.encode("utf-8")).digest()
    return base64.b32encode(digest[:12]).decode("ascii").rstrip("=")[:20]


def parse_payout_request(payload: object) -> tuple[dict[str, Any], str | None]:
    if not isinstance(payload, dict):
        return {}, "body must be a JSON object"
    if "payout_key" in payload:
        return {}, "payout_key is derived server-side and must not be sent"
    tenant_id = payload.get("tenant_id")
    attendant_id = payload.get("attendant_id")
    period = payload.get("payout_period")
    msisdn = payload.get("msisdn")
    amount = payload.get("amount")
    for name, value in (("tenant_id", tenant_id), ("attendant_id", attendant_id)):
        if not isinstance(value, str) or not TEXT_RE.match(value):
            return {}, f"{name} is required (1-64 chars)"
    if not isinstance(period, str) or not TEXT_RE.match(period):
        return {}, "payout_period is required (1-64 chars)"
    if not isinstance(msisdn, str) or not MSISDN_RE.match(msisdn):
        return {}, "msisdn must be 254 followed by 9 digits"
    if not is_int(amount) or amount <= 0:
        return {}, "amount must be a positive integer in minor units"
    return {
        "tenant_id": tenant_id,
        "attendant_id": attendant_id,
        "payout_period": period,
        "msisdn": msisdn,
        "amount_minor": amount,
        "currency": "KES",
    }, None


class PayoutService:
    def __init__(self, settings: Settings, store: Store, adapter: Any, clock: Clock) -> None:
        self.settings = settings
        self.store = store
        self.adapter = adapter
        self.clock = clock

    # Create --------------------------------------------------------------------------------

    def create_payout(self, idem_key: str | None, payload: object) -> Reply:
        if idem_key is None or not IDEMPOTENCY_KEY_RE.match(idem_key):
            return Reply(
                400,
                {
                    "error": "invalid_idempotency_key",
                    "detail": "Idempotency-Key header is required: 16-64 chars of [A-Za-z0-9_-]",
                },
            )
        req, error = parse_payout_request(payload)
        if error:
            return Reply(400, {"error": "invalid_request", "detail": error})
        tenant_id = req["tenant_id"]
        payout_key = derive_payout_key(tenant_id, req["attendant_id"], req["payout_period"])
        fp = fingerprint({**req, "payout_key": payout_key})

        # Replays, mismatches and in-flight duplicates are answered before any business rule or
        # the kill switch, so an accepted request always gets its original answer back.
        with self.store.connection() as conn:
            early = self._idem_reply(
                self.store.idem_lookup(conn, tenant_id, OPERATION, idem_key), fp
            )
        if early is not None:
            return early

        limit = self._limit_error(req["amount_minor"])
        if limit:
            return Reply(422, {"error": limit})
        with self.store.connection() as conn:
            enabled = self.store.get_flag(conn, FLAG_PAYOUTS_ENABLED, self.settings.payouts_enabled)
        if not enabled:
            return Reply(503, {"error": "payouts_disabled"})

        disbursement_id = "dis_" + uuid.uuid4().hex
        originator = originator_id_for(disbursement_id)
        try:
            with self.store.tx() as conn:
                now = self.clock.now()
                early = self._idem_reply(
                    self.store.idem_lookup(conn, tenant_id, OPERATION, idem_key), fp
                )
                if early is not None:
                    return early
                self.store.idem_begin(
                    conn,
                    tenant_id,
                    OPERATION,
                    idem_key,
                    fp,
                    now,
                    self.settings.idempotency_ttl_seconds,
                )
                # Attempt marker and our provider id are committed before the provider call.
                conn.execute(
                    "INSERT INTO disbursements (disbursement_id, tenant_id, idem_key, attendant_id,"
                    " payout_period, payout_key, msisdn, amount_minor, currency,"
                    " originator_conversation_id, state, initiate_started_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?)",
                    (
                        disbursement_id,
                        tenant_id,
                        idem_key,
                        req["attendant_id"],
                        req["payout_period"],
                        payout_key,
                        req["msisdn"],
                        req["amount_minor"],
                        req["currency"],
                        originator,
                        now,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            with self.store.connection() as conn:
                existing = conn.execute(
                    "SELECT disbursement_id FROM disbursements WHERE payout_key = ? AND state <> 'FAILED'",
                    (payout_key,),
                ).fetchone()
            return Reply(
                409,
                {
                    "error": "payout_already_requested",
                    "disbursement_id": existing["disbursement_id"] if existing else None,
                },
            )

        request = DisbursementRequest(
            idempotency_key=adapter_key(tenant_id, OPERATION, idem_key),
            tenant_id=tenant_id,
            originator_conversation_id=originator,
            msisdn=req["msisdn"],
            amount_minor=req["amount_minor"],
            currency=req["currency"],
        )
        accepted = None
        rejected: DisbursementRejectedError | None = None
        try:
            accepted = self.adapter.disburse(request)
        except DisbursementRejectedError as exc:
            rejected = exc
        except Exception as exc:  # noqa: BLE001 (deliberate: any failure means outcome unknown)
            # Timeout, a duplicate-originator answer, or anything unexpected: money may have
            # moved. UNKNOWN, reconcile by our id, never resubmit.
            print(f"disburse outcome unknown for {disbursement_id}: {exc!r}", file=sys.stderr)
        return self._finish_create(disbursement_id, tenant_id, idem_key, accepted, rejected)

    def _limit_error(self, amount_minor: int) -> str | None:
        if amount_minor % 100 != 0:
            return "amount_not_whole_shillings"
        if amount_minor < MIN_DISBURSEMENT_MINOR:
            return "amount_below_minimum"
        if amount_minor > min(self.settings.payout_max_minor, MAX_DISBURSEMENT_MINOR):
            return "amount_above_ceiling"
        return None

    @staticmethod
    def _idem_reply(row: sqlite3.Row | None, fp: str) -> Reply | None:
        verdict = Store.idem_classify(row, fp)
        if verdict == "mismatch":
            return Reply(409, {"error": "idempotency_key_payload_mismatch"})
        if verdict == "replay":
            assert row is not None
            return Reply(200, json.loads(row["response_body"]), {"Idempotent-Replayed": "true"})
        if verdict == "in_flight":
            return Reply(409, {"error": "idempotency_in_flight"}, {"Retry-After": "1"})
        return None

    def _finish_create(
        self,
        disbursement_id: str,
        tenant_id: str,
        idem_key: str,
        accepted: Any,
        rejected: DisbursementRejectedError | None,
    ) -> Reply:
        with self.store.tx() as conn:
            now = self.clock.now()
            row = conn.execute(
                "SELECT * FROM disbursements WHERE disbursement_id = ?", (disbursement_id,)
            ).fetchone()
            state = DisbursementState(row["state"])
            if state is S.CREATED:
                if accepted is not None:
                    self._move(
                        conn,
                        row,
                        S.PENDING,
                        conversation_id=accepted.conversation_id,
                        pending_since=now,
                    )
                elif rejected is not None:
                    self._fail(conn, row, rejected.reason, rejected.raw_code)
                else:
                    self._move(
                        conn,
                        row,
                        S.UNKNOWN,
                        unknown_since=now,
                        next_reconcile_at=now + next_backoff(0),
                    )
            elif accepted is not None:
                conn.execute(
                    "UPDATE disbursements SET conversation_id = ? WHERE disbursement_id = ?",
                    (accepted.conversation_id, disbursement_id),
                )
            body = self._public(conn, disbursement_id)
            self.store.idem_complete(
                conn, tenant_id, OPERATION, idem_key, 201, body, disbursement_id
            )
        return Reply(201, body)

    # Read ----------------------------------------------------------------------------------

    @staticmethod
    def _find(conn: sqlite3.Connection, ident: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM disbursements WHERE disbursement_id = ?"
            " OR originator_conversation_id = ? OR conversation_id = ?",
            (ident, ident, ident),
        ).fetchone()

    @staticmethod
    def _public(conn: sqlite3.Connection, disbursement_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM disbursements WHERE disbursement_id = ?", (disbursement_id,)
        ).fetchone()
        return {
            "disbursement_id": row["disbursement_id"],
            "conversation_id": row["conversation_id"],
            "originator_conversation_id": row["originator_conversation_id"],
            "state": row["state"],
            "failure_reason": row["failure_reason"],
        }

    def get_payout(self, ident: str) -> Reply:
        with self.store.connection() as conn:
            row = self._find(conn, ident)
            if row is None:
                return Reply(404, {"error": "not_found"})
            ledger = conn.execute(
                "SELECT COUNT(*) FROM ledger_entries WHERE record_id = ? AND entry_type = ?",
                (row["disbursement_id"], "DISBURSEMENT_DEBIT"),
            ).fetchone()[0]
            body = self._public(conn, row["disbursement_id"])
        body.update(
            {
                "tenant_id": row["tenant_id"],
                "attendant_id": row["attendant_id"],
                "payout_period": row["payout_period"],
                "amount_minor": row["amount_minor"],
                "msisdn": mask_msisdn(row["msisdn"]),
                "receipt": row["receipt"],
                "ledger_entries": ledger,
            }
        )
        return Reply(200, body)

    # Result callback -----------------------------------------------------------------------

    def handle_result(self, headers: dict[str, str], body: bytes, remote_addr: str) -> Reply:
        if remote_addr not in self.settings.callback_allowed_ips:
            print(f"result from disallowed source {remote_addr}", file=sys.stderr)
            return Reply(403, {"error": "source_not_allowed"})
        try:
            event = self.adapter.parse_disbursement_result(headers, body)
        except CallbackAuthenticityError:
            return Reply(403, {"error": "callback_not_authentic"})
        except CallbackMalformedError as exc:
            with self.store.tx() as conn:
                self.store.anomaly(
                    conn,
                    kind="malformed_callback",
                    severity="warning",
                    now=self.clock.now(),
                    record_type="disbursement",
                    detail=str(exc),
                )
            return Reply(400, {"error": "callback_malformed"})
        try:
            with self.store.tx() as conn:
                result = self._apply_result(conn, event)
        except sqlite3.IntegrityError as exc:
            with self.store.tx() as conn:
                self.store.anomaly(
                    conn,
                    kind="constraint_violation",
                    severity="critical",
                    now=self.clock.now(),
                    record_type="disbursement",
                    provider_ref=event.originator_conversation_id,
                    detail=str(exc),
                )
            result = "conflict"
        return Reply(200, {**ACK, "status": result})

    def _apply_result(self, conn: sqlite3.Connection, event: Any) -> str:
        now = self.clock.now()
        oid = event.originator_conversation_id
        row = conn.execute(
            "SELECT * FROM disbursements WHERE originator_conversation_id = ?", (oid,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO unmatched_callbacks (kind, provider_ref, payload_sha256,"
                " outcome, raw_code, amount_minor, received_at) VALUES ('b2c', ?, ?, ?, ?, ?, ?)",
                (
                    oid,
                    event.payload_sha256,
                    event.outcome.value,
                    event.raw_code,
                    event.amount_minor,
                    now,
                ),
            )
            return "unmatched"
        conn.execute(
            "INSERT OR IGNORE INTO provider_callbacks (kind, provider_ref, payload_sha256, outcome,"
            " raw_code, received_at) VALUES ('b2c', ?, ?, ?, ?, ?)",
            (oid, event.payload_sha256, event.outcome.value, event.raw_code, now),
        )
        state = DisbursementState(row["state"])
        if (
            event.outcome is Outcome.SUCCEEDED
            and not is_terminal("disbursement", state)
            and event.amount_minor != row["amount_minor"]
        ):
            self._to_review(conn, row, "result amount differs from the disbursement amount")
            return "under_review"
        if event.conversation_id and row["conversation_id"] is None:
            conn.execute(
                "UPDATE disbursements SET conversation_id = ? WHERE disbursement_id = ?",
                (event.conversation_id, row["disbursement_id"]),
            )
        return self._apply_outcome(
            conn, row, event.outcome, event.failure_reason, event.receipt, event.raw_code, "result"
        )

    # State application ---------------------------------------------------------------------

    def _move(
        self, conn: sqlite3.Connection, row: sqlite3.Row, target: DisbursementState, **fields: Any
    ) -> None:
        self.store.transition(
            conn,
            kind="disbursement",
            table="disbursements",
            id_column="disbursement_id",
            record_id=row["disbursement_id"],
            current=DisbursementState(row["state"]),
            target=target,
            now=self.clock.now(),
            **fields,
        )

    def _trip(self, conn: sqlite3.Connection, reason: FailureReason, record_id: str) -> None:
        now = self.clock.now()
        self.store.set_flag(conn, FLAG_PAYOUTS_ENABLED, False, reason.value, now)
        self.store.anomaly(
            conn,
            kind="payouts_paused",
            severity="high",
            now=now,
            record_type="disbursement",
            record_id=record_id,
            detail=f"payouts kill switch tripped: {reason.value}",
        )

    def _fail(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        reason: FailureReason,
        raw_code: str | None,
    ) -> None:
        self._move(conn, row, S.FAILED, failure_reason=reason.value, raw_code=raw_code)
        self._event(conn, row, "payout.failed")
        if reason in TRIPPING_REASONS:
            self._trip(conn, reason, row["disbursement_id"])

    def _event(self, conn: sqlite3.Connection, row: sqlite3.Row, event_type: str) -> None:
        self.store.outbox(
            conn,
            row["disbursement_id"],
            event_type,
            {
                "disbursement_id": row["disbursement_id"],
                "tenant_id": row["tenant_id"],
                "attendant_id": row["attendant_id"],
                "payout_period": row["payout_period"],
                "amount_minor": row["amount_minor"],
            },
            self.clock.now(),
        )

    def _to_unknown(self, conn: sqlite3.Connection, row: sqlite3.Row, raw_code: str | None) -> None:
        now = self.clock.now()
        self._move(
            conn,
            row,
            S.UNKNOWN,
            unknown_since=now,
            next_reconcile_at=now + next_backoff(0),
            raw_code=raw_code,
        )

    def _to_review(self, conn: sqlite3.Connection, row: sqlite3.Row, detail: str) -> None:
        state = DisbursementState(row["state"])
        if state is S.CREATED:
            self._move(conn, row, S.PENDING, pending_since=self.clock.now())
            row = conn.execute(
                "SELECT * FROM disbursements WHERE disbursement_id = ?", (row["disbursement_id"],)
            ).fetchone()
            state = S.PENDING
        if state is S.PENDING:
            self._to_unknown(conn, row, None)
            row = conn.execute(
                "SELECT * FROM disbursements WHERE disbursement_id = ?", (row["disbursement_id"],)
            ).fetchone()
            state = S.UNKNOWN
        if state is S.UNKNOWN:
            self._move(conn, row, S.NEEDS_REVIEW)
        self.store.anomaly(
            conn,
            kind="needs_review",
            severity="critical",
            now=self.clock.now(),
            record_type="disbursement",
            record_id=row["disbursement_id"],
            provider_ref=row["originator_conversation_id"],
            from_state=state.value,
            detail=detail,
        )

    def _apply_outcome(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        outcome: Outcome,
        reason: FailureReason | None,
        receipt: str | None,
        raw_code: str | None,
        source: str,
    ) -> str:
        """Apply a provider outcome to a disbursement. The only path to a terminal state."""
        now = self.clock.now()
        state = DisbursementState(row["state"])
        if outcome is Outcome.UNKNOWN:
            if state is S.PENDING:
                self._to_unknown(conn, row, raw_code)
                return "to_unknown"
            return "noop"
        target = S.SUCCEEDED if outcome is Outcome.SUCCEEDED else S.FAILED
        if is_terminal("disbursement", state):
            if state is target:
                return "replay"
            # FAILED then SUCCEEDED means money may have moved: P0 (ADR 0008 section 4).
            self.store.anomaly(
                conn,
                kind="illegal_transition",
                severity="critical",
                now=now,
                record_type="disbursement",
                record_id=row["disbursement_id"],
                provider_ref=row["originator_conversation_id"],
                from_state=state.value,
                event=f"{source}:{outcome.value}",
                detail="terminal state is immutable; a contradicting result was logged, not applied",
            )
            return "illegal_transition_logged"
        if state is S.CREATED:
            # The result raced ahead of the acknowledgement: pass through PENDING.
            self._move(conn, row, S.PENDING, pending_since=now)
            row = conn.execute(
                "SELECT * FROM disbursements WHERE disbursement_id = ?", (row["disbursement_id"],)
            ).fetchone()
        try:
            if target is S.SUCCEEDED:
                self._move(conn, row, S.SUCCEEDED, receipt=receipt, raw_code=raw_code)
            else:
                self._fail(conn, row, reason or FailureReason.REJECTED_AT_INITIATION, raw_code)
                return "applied"
        except IllegalTransition:
            self.store.anomaly(
                conn,
                kind="illegal_transition",
                severity="warning",
                now=now,
                record_type="disbursement",
                record_id=row["disbursement_id"],
                from_state=state.value,
                event=f"{source}:{outcome.value}",
                detail="transition not in the state machine",
            )
            return "illegal_transition_logged"
        self.store.ledger(
            conn,
            tenant_id=row["tenant_id"],
            record_id=row["disbursement_id"],
            entry_type="DISBURSEMENT_DEBIT",
            amount_minor=row["amount_minor"],
            currency=row["currency"],
            provider=PROVIDER,
            provider_ref=row["originator_conversation_id"],
            receipt=receipt,
            now=now,
        )
        self._event(conn, row, "payout.succeeded")
        return "applied"

    # Reconciliation ------------------------------------------------------------------------

    def reconcile_payout(self, ident: str) -> Reply:
        with self.store.connection() as conn:
            row = self._find(conn, ident)
        if row is None:
            return Reply(404, {"error": "not_found"})
        state = DisbursementState(row["state"])
        if state not in (S.UNKNOWN, S.NEEDS_REVIEW):
            return Reply(
                200,
                {
                    "disbursement_id": row["disbursement_id"],
                    "state": state.value,
                    "reconciled": False,
                    "reason": "not_unknown",
                },
            )
        try:
            status = self.adapter.query_disbursement_status(row["originator_conversation_id"])
        except OutcomeUnknownError:
            return self._inconclusive(row["disbursement_id"], "query_timeout")
        except UnknownReferenceError:
            return self._inconclusive(row["disbursement_id"], "provider_does_not_know_id")
        if status.outcome is Outcome.UNKNOWN:
            return self._inconclusive(row["disbursement_id"], "still_unknown")
        with self.store.tx() as conn:
            fresh = conn.execute(
                "SELECT * FROM disbursements WHERE disbursement_id = ?", (row["disbursement_id"],)
            ).fetchone()
            result = self._apply_outcome(
                conn,
                fresh,
                status.outcome,
                status.failure_reason,
                status.receipt,
                status.raw_code,
                "query",
            )
            body = self._public(conn, row["disbursement_id"])
        return Reply(200, {**body, "reconciled": True, "result": result})

    def _inconclusive(self, disbursement_id: str, reason: str) -> Reply:
        with self.store.tx() as conn:
            now = self.clock.now()
            row = conn.execute(
                "SELECT * FROM disbursements WHERE disbursement_id = ?", (disbursement_id,)
            ).fetchone()
            state = DisbursementState(row["state"])
            attempts = row["reconcile_attempts"] + 1
            if state is S.UNKNOWN:
                since = row["unknown_since"] if row["unknown_since"] is not None else now
                if now - since >= self.settings.reconcile_window_seconds:
                    self._to_review(
                        conn, row, "reconcile window elapsed without a definitive answer"
                    )
                    state = S.NEEDS_REVIEW
            conn.execute(
                "UPDATE disbursements SET reconcile_attempts = ?, next_reconcile_at = ?"
                " WHERE disbursement_id = ?",
                (attempts, now + next_backoff(attempts), disbursement_id),
            )
        return Reply(
            200,
            {
                "disbursement_id": disbursement_id,
                "state": state.value,
                "reconciled": False,
                "reason": reason,
            },
        )

    def sweep(self) -> dict[str, int]:
        cfg = self.settings
        counts = {"created_to_unknown": 0, "pending_to_unknown": 0, "reconciled": 0, "checked": 0}
        with self.store.tx() as conn:
            now = self.clock.now()
            for row in conn.execute(
                "SELECT * FROM disbursements WHERE state = 'CREATED' AND initiate_started_at <= ?",
                (now - cfg.created_sweep_seconds,),
            ).fetchall():
                self._to_unknown(conn, row, None)
                self.store.idem_complete(
                    conn,
                    row["tenant_id"],
                    OPERATION,
                    row["idem_key"],
                    201,
                    self._public(conn, row["disbursement_id"]),
                    row["disbursement_id"],
                )
                counts["created_to_unknown"] += 1
            for row in conn.execute(
                "SELECT * FROM disbursements WHERE state = 'PENDING' AND pending_since <= ?",
                (now - cfg.callback_deadline_seconds,),
            ).fetchall():
                self._to_unknown(conn, row, None)
                counts["pending_to_unknown"] += 1
        with self.store.connection() as conn:
            now = self.clock.now()
            due = conn.execute(
                "SELECT disbursement_id FROM disbursements WHERE state IN ('UNKNOWN', 'NEEDS_REVIEW')"
                " AND unknown_since <= ? AND (next_reconcile_at IS NULL OR next_reconcile_at <= ?)",
                (now - cfg.reconcile_sla_seconds, now),
            ).fetchall()
        for row in due:
            counts["checked"] += 1
            if self.reconcile_payout(row["disbursement_id"]).body.get("reconciled"):
                counts["reconciled"] += 1
        return counts
