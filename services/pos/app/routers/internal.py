"""Internal callback Payments uses to tell POS how a sale's payment settled.

Not exposed through API Gateway — service-to-service only (see
docs/threat-model.md for the trust boundary). Idempotent on Payments'
``event_id``: replaying or reordering the same or a conflicting event never
produces a second ledger effect, only an audit row.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..state import InvalidTransition, SaleStatus, transition

router = APIRouter(prefix="/internal", tags=["internal"])


def _record_event(db: Session, sale_id: str, body: schemas.PaymentEventIn) -> None:
    db.add(
        models.PaymentEvent(
            event_id=body.event_id,
            sale_id=sale_id,
            payment_id=body.payment_id,
            status=body.status,
            amount_minor=body.amount_minor,
        )
    )


@router.post("/sales/{sale_id}/payment-events", response_model=schemas.PaymentEventOut)
def record_payment_event(
    sale_id: str, body: schemas.PaymentEventIn, db: Session = Depends(get_db)
) -> schemas.PaymentEventOut:
    sale = db.get(models.Sale, sale_id)
    if sale is None:
        raise HTTPException(status_code=404, detail="sale not found")

    already_seen = db.query(models.PaymentEvent).filter_by(event_id=body.event_id).first()
    if already_seen is not None:
        return schemas.PaymentEventOut(sale_id=sale.id, status=sale.status, applied=False)

    if body.amount_minor != sale.total_minor:
        raise HTTPException(status_code=409, detail="payment amount does not match sale total")

    try:
        new_status = transition(SaleStatus(sale.status), SaleStatus(body.status))
    except InvalidTransition:
        # E.g. a reordered/late callback arriving after the sale already
        # reached a terminal state via an earlier event. Record it for the
        # trace/audit trail, but the sale's status — and any downstream
        # ledger effect — does not move.
        _record_event(db, sale.id, body)
        db.commit()
        return schemas.PaymentEventOut(sale_id=sale.id, status=sale.status, applied=False)

    sale.status = new_status.value
    _record_event(db, sale.id, body)
    try:
        db.commit()
    except IntegrityError:
        # Concurrent delivery of the same event_id — the other request
        # already applied it.
        db.rollback()
        db.refresh(sale)
        return schemas.PaymentEventOut(sale_id=sale.id, status=sale.status, applied=False)

    db.refresh(sale)
    return schemas.PaymentEventOut(sale_id=sale.id, status=sale.status, applied=True)
