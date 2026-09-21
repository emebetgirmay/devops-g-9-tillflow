"""Tenant setup: tills, attendants, products — the owner-configuration side
of the product contract ("Tenant setup — owner configures the till,
attendants, commission rates and tenant-scoped roles")."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db

router = APIRouter(tags=["catalog"])


def get_tenant_or_404(db: Session, tenant_id: str) -> models.Tenant:
    tenant = db.get(models.Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return tenant


@router.post("/tenants", response_model=schemas.TenantOut, status_code=201)
def create_tenant(body: schemas.TenantCreate, db: Session = Depends(get_db)) -> models.Tenant:
    tenant = models.Tenant(name=body.name)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


@router.post("/tenants/{tenant_id}/tills", response_model=schemas.TillOut, status_code=201)
def create_till(tenant_id: str, body: schemas.TillCreate, db: Session = Depends(get_db)) -> models.Till:
    get_tenant_or_404(db, tenant_id)
    till = models.Till(tenant_id=tenant_id, name=body.name)
    db.add(till)
    db.commit()
    db.refresh(till)
    return till


@router.post("/tenants/{tenant_id}/attendants", response_model=schemas.AttendantOut, status_code=201)
def create_attendant(
    tenant_id: str, body: schemas.AttendantCreate, db: Session = Depends(get_db)
) -> models.Attendant:
    get_tenant_or_404(db, tenant_id)
    attendant = models.Attendant(tenant_id=tenant_id, **body.model_dump())
    db.add(attendant)
    db.commit()
    db.refresh(attendant)
    return attendant


@router.post("/tenants/{tenant_id}/products", response_model=schemas.ProductOut, status_code=201)
def create_product(tenant_id: str, body: schemas.ProductCreate, db: Session = Depends(get_db)) -> models.Product:
    get_tenant_or_404(db, tenant_id)
    existing = db.query(models.Product).filter_by(tenant_id=tenant_id, sku=body.sku).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="sku already exists for tenant")
    product = models.Product(tenant_id=tenant_id, **body.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    return product
