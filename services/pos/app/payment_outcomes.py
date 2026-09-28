"""Apply a payment outcome to a sale, idempotently, from whatever triggered it
(a poll of Payments' GET /payments/{id} — the active path, see
``reconcile_sale`` below, called from both app/routers/sales.py's HTTP
endpoint and app/scheduler.py's background sweep — or the internal push
webhook in app/routers/internal.py, kept in case a push model is added
later).

Shared here so every call site gets identical replay/reorder safety: the
same or a conflicting event never produces a second transition or ledger
effect, only an audit row.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from .payments_client import PaymentsClient
from .state import InvalidTransition, SaleStatus, transition

# Payments' terminal states (services/payments/core/states.py::PaymentState)
# mapped onto POS's own sale status. CREATED/PENDING/UNKNOWN/NEEDS_REVIEW are
# deliberately absent — none of them are a verdict, so reconcile_sale leaves
# the sale exactly where it is rather than guessing at one.
PAYMENTS_TERMINAL_STATE_MAP = {
    "SUCCEEDED": SaleStatus.PAID,
    "DECLINED": SaleStatus.PAYMENT_FAILED,
    "EXPIRED": SaleStatus.PAYMENT_FAILED,
}


class ReconcileAmountMismatch(Exception):
    """Payments' amount for this payment doesn't match the sale's own total."""


def apply_outcome(
    db: Session,
    sale: models.Sale,
    *,
    event_id: str,
    payment_id: str,
    target_status: SaleStatus,
) -> tuple[str, bool]:
    """Returns (sale.status after this call, applied).

    ``applied`` is False when event_id was already seen, or when the
    transition was illegal for the sale's current status (e.g. a stale
    DECLINED outcome polled after the sale already reached PAID) — either
    way the event is still recorded for the audit trail.
    """
    already_seen = db.query(models.PaymentEvent).filter_by(event_id=event_id).first()
    if already_seen is not None:
        return sale.status, False

    def record() -> None:
        db.add(
            models.PaymentEvent(
                event_id=event_id,
                sale_id=sale.id,
                payment_id=payment_id,
                status=target_status.value,
                amount_minor=sale.total_minor,
            )
        )

    try:
        new_status = transition(SaleStatus(sale.status), target_status)
    except InvalidTransition:
        record()
        db.commit()
        return sale.status, False

    sale.status = new_status.value
    record()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        db.refresh(sale)
        return sale.status, False

    db.refresh(sale)
    return sale.status, True


def reconcile_sale(
    db: Session, sale: models.Sale, payments_client: PaymentsClient
) -> tuple[str, bool, str]:
    """Poll Payments for sale.payment_id and apply the verdict if it's
    terminal. Returns (sale.status, applied, payments_state).

    Safe to call for a sale that hasn't settled yet: a non-terminal
    Payments state is a no-op here, never a decline — "a timeout is not a
    decline" holds all the way through this function.
    """
    if sale.payment_id is None:
        raise ValueError("sale has no payment requested yet")

    payment = payments_client.get_payment(sale.payment_id)
    payments_state = payment.get("state", "")

    target = PAYMENTS_TERMINAL_STATE_MAP.get(payments_state)
    if target is None:
        return sale.status, False, payments_state

    amount_minor = payment.get("amount_minor")
    if amount_minor is not None and amount_minor != sale.total_minor:
        raise ReconcileAmountMismatch(
            f"payment {sale.payment_id} amount {amount_minor} != sale total {sale.total_minor}"
        )

    # Deterministic per (payment, terminal state): polling again once the
    # outcome is already known is a no-op via event_id dedup in apply_outcome.
    event_id = f"reconcile:{sale.payment_id}:{payments_state}"
    status, applied = apply_outcome(
        db, sale, event_id=event_id, payment_id=sale.payment_id, target_status=target
    )
    return status, applied, payments_state
