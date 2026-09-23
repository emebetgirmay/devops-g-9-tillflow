"""Commission -> B2C: send PLANNED payout_ledger rows to Payments, then reconcile them to a
terminal state (ADR 0008). This is Commission's only path to moving money: it calls Payments'
``POST /payouts`` and ``POST /payouts/{id}/reconcile`` only, through
``ledger/payments_client.py``, and never imports the M-Pesa adapter or holds a Daraja credential
(``tests/test_no_daraja_guard.py`` fails the build if that changes).

Two passes, meant to be run repeatedly (a cron tick, or the CLI subcommands in ``worker.py``):

- ``send_due_payouts``: PLANNED -> REQUESTED, or straight to SUCCEEDED/FAILED if Payments' own
  create response already carries a terminal verdict (a synchronous rejection comes back FAILED
  in that same response). Each ledger row's ``idempotency_key`` is stable (derived once at close
  time from tenant+attendant+business_date), so calling this twice, or running two workers at
  once, asks Payments for the same payout and gets the same answer -- never a second
  disbursement.
- ``reconcile_requested_payouts``: REQUESTED -> SUCCEEDED or FAILED, by asking Payments to check
  with the adapter. Payments only actually queries once its own state is UNKNOWN or NEEDS_REVIEW
  (reached via Payments' own scheduled sweep promoting a stale PENDING disbursement) -- so this
  pass depends on that sweep running somewhere, same as Commission's own close/disburse passes
  need to be scheduled (ADR 0006 open question 9). A non-terminal answer changes nothing here
  either -- the ledger row simply waits for the next reconcile pass, exactly as Payments itself
  waits rather than guessing.
"""

from __future__ import annotations

import time

from ledger.common import classify_payout_response
from ledger.config import Settings
from ledger.payments_client import PaymentsError, create_payout, get_payout, reconcile_payout
from ledger.store import Store

# Same schedule ADR 0006 section 3 documents for Payments' own reconcile backoff, kept local so
# Commission does not import Payments' code for one constant.
BACKOFF_SECONDS = (30, 60, 120, 300, 600, 1800, 3600)

_TERMINAL_PAYMENTS_STATES = {"SUCCEEDED": "SUCCEEDED", "FAILED": "FAILED"}


def _next_backoff(attempts: int) -> int:
    return BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS) - 1)]


def send_due_payouts(store: Store, settings: Settings) -> dict:
    with store.connection() as conn:
        due = conn.execute("SELECT * FROM payout_ledger WHERE state = 'PLANNED'").fetchall()

    counts: dict[str, int] = {}
    aborted: str | None = None
    for row in due:
        body = {
            "tenant_id": row["tenant_id"],
            "attendant_id": row["attendant_id"],
            "payout_period": row["business_date"],
            "msisdn": row["msisdn"],
            "amount": row["amount_minor"],
        }
        try:
            status, reply = create_payout(
                settings.payments_base_url,
                row["idempotency_key"],
                body,
                settings.http_timeout_seconds,
            )
        except PaymentsError as exc:
            aborted = f"payments_unreachable: {exc}"
            counts["not_sent_unreachable"] = counts.get("not_sent_unreachable", 0) + 1
            break

        outcome = classify_payout_response(status, reply)
        counts[outcome] = counts.get(outcome, 0) + 1
        now = time.time()

        if outcome in ("created", "already_requested"):
            # A 200/201 is only "the request was accepted" at the HTTP level -- Payments' own
            # body can already carry a terminal verdict here: a synchronous rejection comes back
            # FAILED in the very same response, and a disburse-time timeout or duplicate-id
            # answer comes back UNKNOWN. Read the real state rather than assuming REQUESTED.
            disbursement_id = reply.get("disbursement_id")
            payments_state = reply.get("state", "")
            target = _TERMINAL_PAYMENTS_STATES.get(payments_state)
            with store.tx() as conn:
                if target is None:
                    conn.execute(
                        "UPDATE payout_ledger SET state = 'REQUESTED', disbursement_id = ?,"
                        " updated_at = ? WHERE id = ? AND state = 'PLANNED'",
                        (disbursement_id, now, row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE payout_ledger SET state = ?, disbursement_id = ?,"
                        " failure_reason = ?, updated_at = ? WHERE id = ? AND state = 'PLANNED'",
                        (target, disbursement_id, reply.get("failure_reason"), now, row["id"]),
                    )
                    if target == "FAILED":
                        Store.anomaly(
                            conn,
                            kind="payout_rejected",
                            severity="critical",
                            now=now,
                            tenant_id=row["tenant_id"],
                            attendant_id=row["attendant_id"],
                            payout_ledger_id=row["id"],
                            detail=f"Payments settled it FAILED at creation: {reply.get('failure_reason')}",
                        )
        elif outcome == "in_flight_retry_next_run":
            continue  # transient; leave PLANNED, try again next pass
        elif outcome == "payouts_disabled":
            with store.tx() as conn:
                Store.anomaly(
                    conn,
                    kind="payouts_paused",
                    severity="high",
                    now=now,
                    tenant_id=row["tenant_id"],
                    attendant_id=row["attendant_id"],
                    payout_ledger_id=row["id"],
                    detail="Payments reports payouts disabled; leaving PLANNED",
                )
            aborted = "payouts_disabled"
            break
        elif outcome.startswith("held_"):
            # Payments rejected the amount or recipient outright: definitively no money moved.
            # Commission's own limits should already match Payments', so this is a real bug to
            # look at, not something to retry blindly.
            with store.tx() as conn:
                conn.execute(
                    "UPDATE payout_ledger SET state = 'FAILED', failure_reason = ?, updated_at = ?"
                    " WHERE id = ? AND state = 'PLANNED'",
                    (outcome, now, row["id"]),
                )
                Store.anomaly(
                    conn,
                    kind="payout_rejected",
                    severity="critical",
                    now=now,
                    tenant_id=row["tenant_id"],
                    attendant_id=row["attendant_id"],
                    payout_ledger_id=row["id"],
                    detail=f"Payments rejected the payout: {outcome}",
                )
        else:  # conflict_needs_review, rejected_* — unexpected, leave PLANNED, flag loudly
            with store.tx() as conn:
                Store.anomaly(
                    conn,
                    kind="unexpected_payout_response",
                    severity="critical",
                    now=now,
                    tenant_id=row["tenant_id"],
                    attendant_id=row["attendant_id"],
                    payout_ledger_id=row["id"],
                    detail=f"status={status} outcome={outcome} reply={reply!r}"[:500],
                )

    return {"checked": len(due), "counts": counts, "aborted": aborted}


def reconcile_requested_payouts(store: Store, settings: Settings) -> dict:
    now = time.time()
    with store.connection() as conn:
        due = conn.execute(
            "SELECT * FROM payout_ledger WHERE state = 'REQUESTED' AND disbursement_id IS NOT NULL"
            " AND (next_reconcile_at IS NULL OR next_reconcile_at <= ?)",
            (now,),
        ).fetchall()

    counts: dict[str, int] = {}
    for row in due:
        try:
            # POST /reconcile triggers Payments to check with the adapter if it is eligible to,
            # but its response omits fields like failure_reason when the row was already
            # terminal (a callback beat us to it) -- follow with a plain GET for the complete,
            # authoritative record rather than relying on the trigger call's own body shape.
            reconcile_payout(
                settings.payments_base_url, row["disbursement_id"], settings.http_timeout_seconds
            )
            payment = get_payout(
                settings.payments_base_url, row["disbursement_id"], settings.http_timeout_seconds
            )
        except PaymentsError:
            counts["inconclusive"] = counts.get("inconclusive", 0) + 1
            _bump_backoff(store, row)
            continue

        payments_state = payment.get("state", "")
        target = _TERMINAL_PAYMENTS_STATES.get(payments_state)
        now = time.time()
        if target is None:
            counts["still_pending"] = counts.get("still_pending", 0) + 1
            _bump_backoff(store, row)
            continue

        counts[target.lower()] = counts.get(target.lower(), 0) + 1
        with store.tx() as conn:
            conn.execute(
                "UPDATE payout_ledger SET state = ?, failure_reason = ?, updated_at = ?"
                " WHERE id = ? AND state = 'REQUESTED'",
                (target, payment.get("failure_reason"), now, row["id"]),
            )
            if target == "SUCCEEDED":
                continue
            Store.anomaly(
                conn,
                kind="payout_failed",
                severity="high",
                now=now,
                tenant_id=row["tenant_id"],
                attendant_id=row["attendant_id"],
                payout_ledger_id=row["id"],
                detail=f"disbursement {row['disbursement_id']} settled FAILED: {payment.get('failure_reason')}",
            )

    return {"checked": len(due), "counts": counts}


def _bump_backoff(store: Store, row) -> None:
    """Real wall-clock time, deliberately: unlike Payments (which takes an injectable clock so
    its own tests can jump time forward), Commission has no clock abstraction, so backoff here
    is genuine elapsed time. A test that wants to see a second reconcile pass pick a row back up
    within the same run needs to either not call reconcile() before that point, or override
    next_reconcile_at directly rather than waiting on the wall clock."""
    attempts = row["reconcile_attempts"] + 1
    with store.tx() as conn:
        conn.execute(
            "UPDATE payout_ledger SET reconcile_attempts = ?, next_reconcile_at = ? WHERE id = ?",
            (attempts, time.time() + _next_backoff(attempts), row["id"]),
        )
