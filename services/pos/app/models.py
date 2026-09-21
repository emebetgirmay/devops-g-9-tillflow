"""SQLAlchemy models for the POS service's own schema.

IDs are server-generated UUID4 strings stored as String(36) rather than a
Postgres-native UUID type, so the same models work unchanged against SQLite
(local/test) and Postgres (deployed) — no dialect-specific column types.
Money is always integer minor units.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def gen_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Till(Base):
    __tablename__ = "tills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class Attendant(Base):
    __tablename__ = "attendants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="ATTENDANT")
    commission_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("tenant_id", "sku", name="uq_product_tenant_sku"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KES")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_sale_tenant_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    till_id: Mapped[str] = mapped_column(String(36), ForeignKey("tills.id"), nullable=False)
    attendant_id: Mapped[str] = mapped_column(String(36), ForeignKey("attendants.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    line_items: Mapped[list["SaleLineItem"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan", order_by="SaleLineItem.id"
    )


class SaleLineItem(Base):
    __tablename__ = "sale_line_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    sale_id: Mapped[str] = mapped_column(String(36), ForeignKey("sales.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(String(36), ForeignKey("products.id"), nullable=False)
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total_minor: Mapped[int] = mapped_column(Integer, nullable=False)

    sale: Mapped[Sale] = relationship(back_populates="line_items")


class PaymentEvent(Base):
    """Audit log of payment-status events received from the Payments API.

    ``event_id`` (assigned by Payments) is unique — replaying or reordering
    the same event is detected here and turned into a no-op rather than a
    second state transition or ledger effect.
    """

    __tablename__ = "payment_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    event_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    sale_id: Mapped[str] = mapped_column(String(36), ForeignKey("sales.id"), nullable=False, index=True)
    payment_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
