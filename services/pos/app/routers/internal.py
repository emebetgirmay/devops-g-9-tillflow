"""Internal callback Payments could use to push a sale's payment outcome.

Not currently wired to anything — Joy's Payments service (services/payments)
is pull-only (GET /payments/{id}, POST /payments/{id}/reconcile), so the
active integration path is app/routers/sales.py::reconcile_payment, which
polls Payments instead. This endpoint is kept in case a push model (e.g.
Payments publishing to an SQS/EventBridge target Platform wires up) lands
later; it shares apply_outcome with the poll path so both get identical
replay/reorder safety.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..payment_outcomes import apply_outcome
from ..state import SaleStatus

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/sales/{sale_id}/payment-events", response_model=schemas.PaymentEventOut)
def record_payment_event(
    sale_id: str, body: schemas.PaymentEventIn, db: Session = Depends(get_db)
) -> schemas.PaymentEventOut:
    sale = db.get(models.Sale, sale_id)
    if sale is None:
        raise HTTPException(status_code=404, detail="sale not found")

    if body.amount_minor != sale.total_minor:
        raise HTTPException(status_code=409, detail="payment amount does not match sale total")

    status, applied = apply_outcome(
        db,
        sale,
        event_id=body.event_id,
        payment_id=body.payment_id,
        target_status=SaleStatus(body.status),
    )
    return schemas.PaymentEventOut(sale_id=sale.id, status=status, applied=applied)
