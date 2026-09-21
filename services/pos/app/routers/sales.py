"""Sale creation, lookup, and payment request — the core product flow:

    CREATE SALE -> SALE ID -> TOTAL -> READY_FOR_PAYMENT -> (Payments) PAID

All line-item pricing is computed server-side from the tenant's product
catalog; the client sends only product_id + quantity, never a price. That's
the validation boundary that keeps a sale's total trustworthy.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..payments_client import PaymentsClient, get_payments_client
from ..state import InvalidTransition, SaleStatus, transition
from .catalog import get_tenant_or_404

router = APIRouter(tags=["sales"])


def _find_sale_by_idempotency_key(db: Session, tenant_id: str, idempotency_key: str) -> models.Sale | None:
    return db.query(models.Sale).filter_by(tenant_id=tenant_id, idempotency_key=idempotency_key).first()


def _get_sale_or_404(db: Session, tenant_id: str, sale_id: str) -> models.Sale:
    sale = db.get(models.Sale, sale_id)
    if sale is None or sale.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="sale not found")
    return sale


@router.post("/tenants/{tenant_id}/sales", response_model=schemas.SaleOut, status_code=201)
def create_sale(
    tenant_id: str,
    body: schemas.SaleCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
) -> models.Sale:
    get_tenant_or_404(db, tenant_id)

    existing = _find_sale_by_idempotency_key(db, tenant_id, idempotency_key)
    if existing is not None:
        return existing

    till = db.get(models.Till, body.till_id)
    if till is None or till.tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="till does not belong to tenant")

    attendant = db.get(models.Attendant, body.attendant_id)
    if attendant is None or attendant.tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="attendant does not belong to tenant")
    if not attendant.active:
        raise HTTPException(status_code=400, detail="attendant is not active")

    line_items: list[models.SaleLineItem] = []
    total_minor = 0
    currency: str | None = None
    for item in body.line_items:
        product = db.get(models.Product, item.product_id)
        if product is None or product.tenant_id != tenant_id:
            raise HTTPException(status_code=400, detail=f"product {item.product_id} does not belong to tenant")
        if not product.active:
            raise HTTPException(status_code=400, detail=f"product {item.product_id} is not active")
        if currency is None:
            currency = product.currency
        elif currency != product.currency:
            raise HTTPException(status_code=400, detail="mixed currencies in one sale are not supported")

        line_total = product.price_minor * item.quantity
        total_minor += line_total
        line_items.append(
            models.SaleLineItem(
                product_id=product.id,
                product_name=product.name,
                quantity=item.quantity,
                unit_price_minor=product.price_minor,
                line_total_minor=line_total,
            )
        )

    sale = models.Sale(
        tenant_id=tenant_id,
        till_id=till.id,
        attendant_id=attendant.id,
        idempotency_key=idempotency_key,
        status=SaleStatus.READY_FOR_PAYMENT.value,
        currency=currency,
        total_minor=total_minor,
        line_items=line_items,
    )
    db.add(sale)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race against a concurrent identical retry (same tenant +
        # Idempotency-Key) — the unique constraint caught it; return the
        # sale the winner created instead of a duplicate.
        db.rollback()
        winner = _find_sale_by_idempotency_key(db, tenant_id, idempotency_key)
        if winner is None:
            raise
        return winner
    db.refresh(sale)
    return sale


@router.get("/tenants/{tenant_id}/sales/{sale_id}", response_model=schemas.SaleOut)
def get_sale(tenant_id: str, sale_id: str, db: Session = Depends(get_db)) -> models.Sale:
    return _get_sale_or_404(db, tenant_id, sale_id)


@router.post("/tenants/{tenant_id}/sales/{sale_id}/payment-request", response_model=schemas.PaymentRequestOut)
def request_payment(
    tenant_id: str,
    sale_id: str,
    body: schemas.PaymentRequestIn,
    db: Session = Depends(get_db),
    payments_client: PaymentsClient = Depends(get_payments_client),
) -> schemas.PaymentRequestOut:
    sale = _get_sale_or_404(db, tenant_id, sale_id)

    try:
        new_status = transition(SaleStatus(sale.status), SaleStatus.PAYMENT_REQUESTED)
    except InvalidTransition:
        raise HTTPException(
            status_code=409, detail=f"cannot request payment for sale in status {sale.status}"
        ) from None

    # sale.id doubles as the idempotency key Payments uses for this STK
    # push: one sale can only ever have one in-flight payment request.
    result = payments_client.request_payment(
        sale_id=sale.id,
        tenant_id=tenant_id,
        amount_minor=sale.total_minor,
        currency=sale.currency,
        phone=body.phone,
        idempotency_key=sale.id,
    )

    sale.status = new_status.value
    sale.payment_id = result["payment_id"]
    db.commit()
    db.refresh(sale)
    return schemas.PaymentRequestOut(sale_id=sale.id, payment_id=sale.payment_id, status=sale.status)
