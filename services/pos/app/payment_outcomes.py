"""Apply a payment outcome to a sale, idempotently, from whatever triggered it
(a poll of Payments' GET /payments/{id} — the active path, see
app/routers/sales.py::reconcile_payment — or the internal push webhook in
app/routers/internal.py, kept in case a push model is added later).

Shared here so both call sites get identical replay/reorder safety: the same
or a conflicting event never produces a second transition or ledger effect,
only an audit row.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from .state import InvalidTransition, SaleStatus, transition


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
