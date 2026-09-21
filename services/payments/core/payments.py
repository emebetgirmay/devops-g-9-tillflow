"""Payments: idempotent STK charges, callbacks and reconciliation (ADR 0006).

Rules this module enforces and the tests prove:
- the same (tenant, operation, key) with the same payload returns the original response, with a
  different payload a 409, and a concurrent duplicate a 409 in flight, never a second charge;
- a timeout is never a decline and never triggers a second initiate call: it becomes UNKNOWN and is
  reconciled by the status query, then escalated to NEEDS_REVIEW after the window, never auto-failed;
- a callback replayed any number of times, late, or out of order has exactly one ledger effect,
  and a terminal state never changes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from typing import Any

from mpesa import (
    CallbackAuthenticityError,
    CallbackMalformedError,
    ChargeDeclinedError,
    ChargeRequest,
    DeclineReason,
    Outcome,
    OutcomeUnknownError,
    UnknownReferenceError,
)
from mpesa.fake_common import Clock
from mpesa.models import IDEMPOTENCY_KEY_RE, MSISDN_RE

from core.common import (
    PROVIDER,
    TEXT_RE,
    Reply,
    adapter_key,
    fingerprint,
    is_int,
    mask_msisdn,
    next_backoff,
)
from core.config import Settings
from core.states import IllegalTransition, PaymentState, is_terminal
from core.store import StaleStateError, Store

OPERATION = "payment.create"
ACK = {"ResultCode": 0, "ResultDesc": "Accepted"}


def parse_payment_request(payload: object) -> tuple[dict[str, Any], str | None]:
    """Validate the body. amount is an integer in minor units (cents)."""
    if not isinstance(payload, dict):
        return {}, "body must be a JSON object"
    tenant_id = payload.get("tenant_id")
    msisdn = payload.get("msisdn")
    amount = payload.get("amount")
    reference = payload.get("account_reference", "")
    sale_id = payload.get("sale_id")
    currency = payload.get("currency", "KES")
    if not isinstance(tenant_id, str) or not TEXT_RE.match(tenant_id):
        return {}, "tenant_id is required (1-64 chars)"
    if not isinstance(msisdn, str) or not MSISDN_RE.match(msisdn):
        return {}, "msisdn must be 254 followed by 9 digits"
    if not is_int(amount) or amount <= 0:
        return {}, "amount must be a positive integer in minor units"
    if not isinstance(reference, str) or len(reference) > 12:
        return {}, "account_reference must be at most 12 characters"
    if sale_id is not None and (not isinstance(sale_id, str) or not TEXT_RE.match(sale_id)):
        return {}, "sale_id must be 1-64 chars when given"
    if currency != "KES":
        return {}, "only KES is supported"
    return {
        "tenant_id": tenant_id,
        "msisdn": msisdn,
        "amount_minor": amount,
        "reference": reference,
        "sale_id": sale_id,
        "currency": currency,
    }, None


class PaymentService:
    def __init__(self, settings: Settings, store: Store, adapter: Any, clock: Clock) -> None:
        self.settings = settings
        self.store = store
        self.adapter = adapter
        self.clock = clock

    # Create --------------------------------------------------------------------------------

    def create_payment(self, idem_key: str | None, payload: object) -> Reply:
        if idem_key is None or not IDEMPOTENCY_KEY_RE.match(idem_key):
            return Reply(
                400,
                {
                    "error": "invalid_idempotency_key",
                    "detail": "Idempotency-Key header is required: 16-64 chars of [A-Za-z0-9_-]",
                },
            )
        req, error = parse_payment_request(payload)
        if error:
            return Reply(400, {"error": "invalid_request", "detail": error})

        tenant_id = req["tenant_id"]
        fp = fingerprint(
            {
                "tenant_id": tenant_id,
                "sale_id": req["sale_id"],
                "amount_minor": req["amount_minor"],
                "currency": req["currency"],
                "msisdn": req["msisdn"],
            }
        )
        payment_id = "pay_" + uuid.uuid4().hex
        try:
            with self.store.tx() as conn:
                now = self.clock.now()
                row = self.store.idem_lookup(conn, tenant_id, OPERATION, idem_key)
                verdict = self.store.idem_classify(row, fp)
                if verdict == "mismatch":
                    return Reply(409, {"error": "idempotency_key_payload_mismatch"})
                if verdict == "replay":
                    assert row is not None
                    return Reply(
                        200, json.loads(row["response_body"]), {"Idempotent-Replayed": "true"}
                    )
                if verdict == "in_flight":
                    return Reply(409, {"error": "idempotency_in_flight"}, {"Retry-After": "1"})
                self.store.idem_begin(
                    conn,
                    tenant_id,
                    OPERATION,
                    idem_key,
                    fp,
                    now,
                    self.settings.idempotency_ttl_seconds,
                )
                # The attempt marker is committed with the intent, before the provider call, so a
                # crash after this point is swept to UNKNOWN and never re-sent.
                conn.execute(
                    "INSERT INTO payments (payment_id, tenant_id, idem_key, sale_id, msisdn,"
                    " amount_minor, currency, reference, state, initiate_started_at, created_at,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?)",
                    (
                        payment_id,
                        tenant_id,
                        idem_key,
                        req["sale_id"],
                        req["msisdn"],
                        req["amount_minor"],
                        req["currency"],
                        req["reference"],
                        now,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            return Reply(409, {"error": "sale_already_has_live_payment"})

        charge = ChargeRequest(
            idempotency_key=adapter_key(tenant_id, OPERATION, idem_key),
            tenant_id=tenant_id,
            msisdn=req["msisdn"],
            amount_minor=req["amount_minor"],
            currency=req["currency"],
            reference=req["reference"],
        )
        accepted = None
        declined: ChargeDeclinedError | None = None
        try:
            accepted = self.adapter.initiate_charge(charge)
        except ChargeDeclinedError as exc:
            declined = exc
        except Exception as exc:  # noqa: BLE001 (deliberate: any failure means outcome unknown)
            # Timeout, transport failure or anything unexpected: a charge may exist. Never treat
            # as a decline and never retry the initiate call (ADR 0006 section 3).
            print(f"initiate outcome unknown for {payment_id}: {exc!r}", file=sys.stderr)

        return self._finish_create(payment_id, tenant_id, idem_key, accepted, declined)

    def _finish_create(
        self,
        payment_id: str,
        tenant_id: str,
        idem_key: str,
        accepted: Any,
        declined: ChargeDeclinedError | None,
    ) -> Reply:
        with self.store.tx() as conn:
            now = self.clock.now()
            row = conn.execute(
                "SELECT * FROM payments WHERE payment_id = ?", (payment_id,)
            ).fetchone()
            state = PaymentState(row["state"])
            if state is PaymentState.CREATED:
                if accepted is not None:
                    self.store.transition(
                        conn,
                        kind="payment",
                        table="payments",
                        id_column="payment_id",
                        record_id=payment_id,
                        current=state,
                        target=PaymentState.PENDING,
                        now=now,
                        provider_ref=accepted.provider_ref,
                        merchant_request_id=accepted.merchant_request_id,
                        pending_since=now,
                    )
                elif declined is not None:
                    self.store.transition(
                        conn,
                        kind="payment",
                        table="payments",
                        id_column="payment_id",
                        record_id=payment_id,
                        current=state,
                        target=PaymentState.DECLINED,
                        now=now,
                        decline_reason=DeclineReason.REJECTED_AT_INITIATION.value,
                        raw_code=declined.raw_code,
                    )
                    self._event(conn, row, "payment.declined", now)
                else:
                    self.store.transition(
                        conn,
                        kind="payment",
                        table="payments",
                        id_column="payment_id",
                        record_id=payment_id,
                        current=state,
                        target=PaymentState.UNKNOWN,
                        now=now,
                        unknown_since=now,
                        next_reconcile_at=now + next_backoff(0),
                    )
            elif accepted is not None:
                # The sweeper already moved this payment to UNKNOWN; keep the reference so it can
                # still be reconciled by query.
                conn.execute(
                    "UPDATE payments SET provider_ref = ?, merchant_request_id = ? WHERE payment_id = ?",
                    (accepted.provider_ref, accepted.merchant_request_id, payment_id),
                )
            body = self._public(conn, payment_id)
            self.store.idem_complete(conn, tenant_id, OPERATION, idem_key, 201, body, payment_id)
        return Reply(201, body)

    # Read ----------------------------------------------------------------------------------

    @staticmethod
    def _find(conn: sqlite3.Connection, ident: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM payments WHERE payment_id = ? OR provider_ref = ?", (ident, ident)
        ).fetchone()

    def _public(self, conn: sqlite3.Connection, payment_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,)).fetchone()
        return {
            "payment_id": row["payment_id"],
            "checkout_request_id": row["provider_ref"],
            "state": row["state"],
            "decline_reason": row["decline_reason"],
        }

    def get_payment(self, ident: str) -> Reply:
        with self.store.connection() as conn:
            row = self._find(conn, ident)
            if row is None:
                return Reply(404, {"error": "not_found"})
            ledger = conn.execute(
                "SELECT COUNT(*) FROM ledger_entries WHERE record_id = ? AND entry_type = ?",
                (row["payment_id"], "PAYMENT_CREDIT"),
            ).fetchone()[0]
            body = self._public(conn, row["payment_id"])
        body.update(
            {
                "tenant_id": row["tenant_id"],
                "sale_id": row["sale_id"],
                "amount_minor": row["amount_minor"],
                "currency": row["currency"],
                "msisdn": mask_msisdn(row["msisdn"]),
                "receipt": row["receipt"],
                "ledger_entries": ledger,
            }
        )
        return Reply(200, body)

    # Callback ------------------------------------------------------------------------------

    def handle_callback(self, headers: dict[str, str], body: bytes, remote_addr: str) -> Reply:
        if remote_addr not in self.settings.callback_allowed_ips:
            print(f"callback from disallowed source {remote_addr}", file=sys.stderr)
            return Reply(403, {"error": "source_not_allowed"})
        try:
            event = self.adapter.parse_callback(headers, body)
        except CallbackAuthenticityError:
            return Reply(403, {"error": "callback_not_authentic"})
        except CallbackMalformedError as exc:
            with self.store.tx() as conn:
                self.store.anomaly(
                    conn,
                    kind="malformed_callback",
                    severity="warning",
                    now=self.clock.now(),
                    record_type="payment",
                    detail=str(exc),
                )
            return Reply(400, {"error": "callback_malformed"})

        with self.store.connection() as conn:
            existing = conn.execute(
                "SELECT * FROM payments WHERE provider_ref = ?", (event.provider_ref,)
            ).fetchone()

        confirmed: str | None = None
        if (
            existing is not None
            and event.outcome is Outcome.SUCCEEDED
            and not is_terminal("payment", PaymentState(existing["state"]))
            and self.settings.confirm_success_with_query
        ):
            confirmed = self._confirm_success(event.provider_ref)

        try:
            with self.store.tx() as conn:
                result = self._apply_callback(conn, event, confirmed)
        except sqlite3.IntegrityError as exc:
            with self.store.tx() as conn:
                self.store.anomaly(
                    conn,
                    kind="constraint_violation",
                    severity="critical",
                    now=self.clock.now(),
                    record_type="payment",
                    provider_ref=event.provider_ref,
                    detail=str(exc),
                )
            result = "conflict"
        return Reply(200, {**ACK, "status": result})

    def _confirm_success(self, provider_ref: str) -> str:
        """Confirm a success callback with a status query. Returns 'confirmed', 'contradicted'
        or 'inconclusive'."""
        try:
            status = self.adapter.query_status(provider_ref)
        except (OutcomeUnknownError, UnknownReferenceError):
            return "inconclusive"
        if status.outcome is Outcome.SUCCEEDED:
            return "confirmed"
        if status.outcome in (Outcome.DECLINED, Outcome.EXPIRED):
            return "contradicted"
        return "inconclusive"

    def _apply_callback(self, conn: sqlite3.Connection, event: Any, confirmed: str | None) -> str:
        now = self.clock.now()
        payment = conn.execute(
            "SELECT * FROM payments WHERE provider_ref = ?", (event.provider_ref,)
        ).fetchone()
        if payment is None:
            conn.execute(
                "INSERT OR IGNORE INTO unmatched_callbacks (kind, provider_ref, payload_sha256,"
                " outcome, raw_code, amount_minor, received_at) VALUES ('stk', ?, ?, ?, ?, ?, ?)",
                (
                    event.provider_ref,
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
            " raw_code, received_at) VALUES ('stk', ?, ?, ?, ?, ?)",
            (event.provider_ref, event.payload_sha256, event.outcome.value, event.raw_code, now),
        )
        state = PaymentState(payment["state"])
        if event.outcome is Outcome.SUCCEEDED and not is_terminal("payment", state):
            if event.amount_minor != payment["amount_minor"]:
                self._to_review(conn, payment, "callback amount differs from the payment amount")
                return "under_review"
            if confirmed == "contradicted":
                self.store.anomaly(
                    conn,
                    kind="unconfirmed_success",
                    severity="critical",
                    now=now,
                    record_type="payment",
                    record_id=payment["payment_id"],
                    provider_ref=event.provider_ref,
                    from_state=state.value,
                    event="callback_success",
                    detail="status query contradicts the success callback",
                )
                return "contradicted"
            if confirmed == "inconclusive":
                if state is PaymentState.PENDING:
                    self._to_unknown(conn, payment, event.raw_code)
                return "unconfirmed"
        return self._apply_outcome(
            conn,
            payment,
            event.outcome,
            event.decline_reason,
            event.receipt,
            event.raw_code,
            "callback",
        )

    # State application (shared by callbacks and reconciliation) ----------------------------

    def _to_unknown(
        self, conn: sqlite3.Connection, payment: sqlite3.Row, raw_code: str | None
    ) -> None:
        now = self.clock.now()
        self.store.transition(
            conn,
            kind="payment",
            table="payments",
            id_column="payment_id",
            record_id=payment["payment_id"],
            current=PaymentState(payment["state"]),
            target=PaymentState.UNKNOWN,
            now=now,
            unknown_since=now,
            next_reconcile_at=now + next_backoff(0),
            raw_code=raw_code,
        )

    def _to_review(self, conn: sqlite3.Connection, payment: sqlite3.Row, detail: str) -> None:
        now = self.clock.now()
        state = PaymentState(payment["state"])
        if state is PaymentState.PENDING:
            self._to_unknown(conn, payment, None)
            state = PaymentState.UNKNOWN
        if state is PaymentState.UNKNOWN:
            self.store.transition(
                conn,
                kind="payment",
                table="payments",
                id_column="payment_id",
                record_id=payment["payment_id"],
                current=state,
                target=PaymentState.NEEDS_REVIEW,
                now=now,
            )
        self.store.anomaly(
            conn,
            kind="needs_review",
            severity="high",
            now=now,
            record_type="payment",
            record_id=payment["payment_id"],
            provider_ref=payment["provider_ref"],
            from_state=state.value,
            detail=detail,
        )

    def _event(
        self, conn: sqlite3.Connection, payment: sqlite3.Row, event_type: str, now: float
    ) -> None:
        self.store.outbox(
            conn,
            payment["payment_id"],
            event_type,
            {
                "payment_id": payment["payment_id"],
                "tenant_id": payment["tenant_id"],
                "amount_minor": payment["amount_minor"],
            },
            now,
        )

    def _apply_outcome(
        self,
        conn: sqlite3.Connection,
        payment: sqlite3.Row,
        outcome: Outcome,
        reason: DeclineReason | None,
        receipt: str | None,
        raw_code: str | None,
        source: str,
    ) -> str:
        """Apply a provider outcome to a payment. Returns 'applied', 'replay', 'noop', 'to_unknown'
        or 'illegal_transition_logged'. The only path that moves a payment to a terminal state."""
        now = self.clock.now()
        state = PaymentState(payment["state"])
        if outcome is Outcome.UNKNOWN:
            # No claim about the money: never terminal. A pending payment becomes UNKNOWN.
            if state is PaymentState.PENDING:
                self._to_unknown(conn, payment, raw_code)
                return "to_unknown"
            return "noop"
        target = {
            Outcome.SUCCEEDED: PaymentState.SUCCEEDED,
            Outcome.DECLINED: PaymentState.DECLINED,
            Outcome.EXPIRED: PaymentState.EXPIRED,
        }[outcome]
        if is_terminal("payment", state):
            if state is target:
                return "replay"
            self.store.anomaly(
                conn,
                kind="illegal_transition",
                severity="critical" if PaymentState.SUCCEEDED in (state, target) else "warning",
                now=now,
                record_type="payment",
                record_id=payment["payment_id"],
                provider_ref=payment["provider_ref"],
                from_state=state.value,
                event=f"{source}:{outcome.value}",
                detail="terminal state is immutable; a contradicting result was logged, not applied",
            )
            return "illegal_transition_logged"
        fields: dict[str, Any] = {"raw_code": raw_code}
        if target is PaymentState.SUCCEEDED:
            fields["receipt"] = receipt
        elif target is PaymentState.DECLINED:
            fields["decline_reason"] = reason.value if reason else None
        try:
            self.store.transition(
                conn,
                kind="payment",
                table="payments",
                id_column="payment_id",
                record_id=payment["payment_id"],
                current=state,
                target=target,
                now=now,
                **fields,
            )
        except IllegalTransition:
            self.store.anomaly(
                conn,
                kind="illegal_transition",
                severity="warning",
                now=now,
                record_type="payment",
                record_id=payment["payment_id"],
                provider_ref=payment["provider_ref"],
                from_state=state.value,
                event=f"{source}:{outcome.value}",
                detail="transition not in the state machine",
            )
            return "illegal_transition_logged"
        if target is PaymentState.SUCCEEDED:
            self.store.ledger(
                conn,
                tenant_id=payment["tenant_id"],
                record_id=payment["payment_id"],
                entry_type="PAYMENT_CREDIT",
                amount_minor=payment["amount_minor"],
                currency=payment["currency"],
                provider=PROVIDER,
                provider_ref=payment["provider_ref"],
                receipt=receipt,
                now=now,
            )
            self._event(conn, payment, "payment.succeeded", now)
        elif target is PaymentState.DECLINED:
            self._event(conn, payment, "payment.declined", now)
        else:
            self._event(conn, payment, "payment.expired", now)
        return "applied"

    # Reconciliation ------------------------------------------------------------------------

    def reconcile_payment(self, ident: str) -> Reply:
        with self.store.connection() as conn:
            row = self._find(conn, ident)
        if row is None:
            return Reply(404, {"error": "not_found"})
        state = PaymentState(row["state"])
        if state not in (PaymentState.UNKNOWN, PaymentState.NEEDS_REVIEW):
            return Reply(
                200,
                {
                    "payment_id": row["payment_id"],
                    "state": state.value,
                    "reconciled": False,
                    "reason": "not_unknown",
                },
            )
        if row["provider_ref"] is None:
            return self._inconclusive(row["payment_id"], "no_provider_reference")
        try:
            status = self.adapter.query_status(row["provider_ref"])
        except OutcomeUnknownError:
            return self._inconclusive(row["payment_id"], "query_timeout")
        except UnknownReferenceError:
            return self._inconclusive(row["payment_id"], "provider_does_not_know_reference")
        if status.outcome is Outcome.UNKNOWN:
            return self._inconclusive(row["payment_id"], "still_unknown")
        with self.store.tx() as conn:
            fresh = conn.execute(
                "SELECT * FROM payments WHERE payment_id = ?", (row["payment_id"],)
            ).fetchone()
            result = self._apply_outcome(
                conn,
                fresh,
                status.outcome,
                status.decline_reason,
                status.receipt,
                status.raw_code,
                "query",
            )
            body = self._public(conn, row["payment_id"])
        return Reply(200, {**body, "reconciled": True, "result": result})

    def _inconclusive(self, payment_id: str, reason: str) -> Reply:
        with self.store.tx() as conn:
            now = self.clock.now()
            row = conn.execute(
                "SELECT * FROM payments WHERE payment_id = ?", (payment_id,)
            ).fetchone()
            state = PaymentState(row["state"])
            attempts = row["reconcile_attempts"] + 1
            next_at = now + next_backoff(attempts)
            if state is PaymentState.UNKNOWN:
                since = row["unknown_since"] if row["unknown_since"] is not None else now
                if now - since >= self.settings.reconcile_window_seconds:
                    self._to_review(
                        conn, row, "reconcile window elapsed without a definitive answer"
                    )
                    state = PaymentState.NEEDS_REVIEW
            conn.execute(
                "UPDATE payments SET reconcile_attempts = ?, next_reconcile_at = ? WHERE payment_id = ?",
                (attempts, next_at, payment_id),
            )
        return Reply(
            200,
            {"payment_id": payment_id, "state": state.value, "reconciled": False, "reason": reason},
        )

    def sweep(self) -> dict[str, int]:
        """One reconcile pass: expire lost initiate attempts and callbacks to UNKNOWN, then
        query every UNKNOWN payment that is due. Idempotent and safe to run repeatedly."""
        cfg = self.settings
        counts = {"created_to_unknown": 0, "pending_to_unknown": 0, "reconciled": 0, "checked": 0}
        with self.store.tx() as conn:
            now = self.clock.now()
            for row in conn.execute(
                "SELECT * FROM payments WHERE state = 'CREATED' AND initiate_started_at <= ?",
                (now - cfg.created_sweep_seconds,),
            ).fetchall():
                self.store.transition(
                    conn,
                    kind="payment",
                    table="payments",
                    id_column="payment_id",
                    record_id=row["payment_id"],
                    current=PaymentState.CREATED,
                    target=PaymentState.UNKNOWN,
                    now=now,
                    unknown_since=now,
                    next_reconcile_at=now + next_backoff(0),
                )
                self.store.idem_complete(
                    conn,
                    row["tenant_id"],
                    OPERATION,
                    row["idem_key"],
                    201,
                    self._public(conn, row["payment_id"]),
                    row["payment_id"],
                )
                counts["created_to_unknown"] += 1
            for row in conn.execute(
                "SELECT * FROM payments WHERE state = 'PENDING' AND pending_since <= ?",
                (now - cfg.callback_deadline_seconds,),
            ).fetchall():
                self._to_unknown(conn, row, None)
                counts["pending_to_unknown"] += 1
        with self.store.connection() as conn:
            now = self.clock.now()
            due = conn.execute(
                "SELECT payment_id FROM payments WHERE state IN ('UNKNOWN', 'NEEDS_REVIEW')"
                " AND unknown_since <= ? AND (next_reconcile_at IS NULL OR next_reconcile_at <= ?)",
                (now - cfg.reconcile_sla_seconds, now),
            ).fetchall()
        for row in due:
            counts["checked"] += 1
            result = self.reconcile_payment(row["payment_id"])
            if result.body.get("reconciled"):
                counts["reconciled"] += 1
        return counts


__all__ = ["ACK", "OPERATION", "PaymentService", "StaleStateError", "parse_payment_request"]
