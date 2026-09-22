"""Read-only endpoint for Commission: confirmed-paid sales, ready to compute commission from.

Fills the gap ``services/_shared/pos-payments-contract.md`` names under "Commission input gap":
Commission calculates only from confirmed **paid** sales (architecture.md invariant 4), and only
POS knows which sales are ``PAID`` and what each attendant's ``commission_rate_bps`` is. This
endpoint is additive — it does not touch sales.py, catalog.py or any existing route — and
read-only: it cannot move a sale out of ``PAID`` or write anything.

A sale becomes eligible the moment it reaches ``PAID`` (state.py has no further transitions out of
it, so ``updated_at`` at that point is stable and doubles as ``paid_at``). Pagination is a keyset
cursor on ``(updated_at, id)`` so a page boundary landing between two sales with the same
timestamp never skips or repeats one — a duplicate page result is still safe on Commission's side
(``payout_items`` has ``UNIQUE(sale_id)``), but this endpoint tries not to hand out one anyway.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..state import SaleStatus
from .catalog import get_tenant_or_404

router = APIRouter(prefix="/tenants/{tenant_id}/commission", tags=["commission"])

DEFAULT_LIMIT = 500
MAX_LIMIT = 2000


def _encode_cursor(updated_at: datetime, sale_id: str) -> str:
    raw = f"{updated_at.isoformat()}|{sale_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode()
        iso, sale_id = raw.rsplit("|", 1)
        dt = datetime.fromisoformat(iso)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt, sale_id


@router.get("/paid-sales", response_model=schemas.PaidSalesPage)
def list_paid_sales(
    tenant_id: str,
    since: datetime = Query(..., description="Inclusive lower bound on paid_at (sale.updated_at)"),
    until: datetime = Query(..., description="Exclusive upper bound on paid_at"),
    attendant_id: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, gt=0, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> schemas.PaidSalesPage:
    get_tenant_or_404(db, tenant_id)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if until <= since:
        raise HTTPException(status_code=400, detail="until must be after since")

    query = (
        db.query(models.Sale, models.Attendant.commission_rate_bps, models.Attendant.phone)
        .join(models.Attendant, models.Attendant.id == models.Sale.attendant_id)
        .filter(
            models.Sale.tenant_id == tenant_id,
            models.Sale.status == SaleStatus.PAID.value,
            models.Sale.updated_at >= since,
            models.Sale.updated_at < until,
        )
    )
    if attendant_id is not None:
        query = query.filter(models.Sale.attendant_id == attendant_id)
    if cursor is not None:
        cursor_dt, cursor_id = _decode_cursor(cursor)
        query = query.filter(
            tuple_(models.Sale.updated_at, models.Sale.id) > (cursor_dt, cursor_id)
        )

    rows = query.order_by(models.Sale.updated_at, models.Sale.id).limit(limit + 1).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        schemas.PaidSaleOut(
            sale_id=sale.id,
            attendant_id=sale.attendant_id,
            attendant_msisdn=phone,
            commission_rate_bps=rate_bps,
            total_minor=sale.total_minor,
            currency=sale.currency,
            paid_at=sale.updated_at,
        )
        for sale, rate_bps, phone in rows
    ]
    next_cursor = (
        _encode_cursor(rows[-1][0].updated_at, rows[-1][0].id) if has_more and rows else None
    )
    return schemas.PaidSalesPage(sales=items, next_cursor=next_cursor)
