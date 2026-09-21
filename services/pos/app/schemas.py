"""Pydantic request/response contracts for the POS API.

These are also the API contract the web frontend and evidence demos build
against — see ``services/_shared/pos-payments-contract.md`` for the subset
shared with the Payments service specifically.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

PHONE_PATTERN = r"^254\d{9}$"


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str


class TillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    tenant_id: str
    name: str


class AttendantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(pattern=PHONE_PATTERN)
    role: str = Field(default="ATTENDANT")
    commission_rate_bps: int = Field(default=0, ge=0, le=10_000)

    @field_validator("role")
    @classmethod
    def role_must_be_known(cls, v: str) -> str:
        if v not in {"OWNER", "ATTENDANT"}:
            raise ValueError("role must be OWNER or ATTENDANT")
        return v


class AttendantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    tenant_id: str
    name: str
    phone: str
    role: str
    commission_rate_bps: int
    active: bool


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    price_minor: int = Field(gt=0)
    currency: str = Field(default="KES", min_length=3, max_length=3)


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    tenant_id: str
    sku: str
    name: str
    price_minor: int
    currency: str
    active: bool


class SaleLineItemIn(BaseModel):
    product_id: str
    quantity: int = Field(gt=0, le=10_000)


class SaleCreate(BaseModel):
    till_id: str
    attendant_id: str
    line_items: list[SaleLineItemIn] = Field(min_length=1, max_length=200)


class SaleLineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: str
    product_name: str
    quantity: int
    unit_price_minor: int
    line_total_minor: int


class SaleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    tenant_id: str
    till_id: str
    attendant_id: str
    status: str
    currency: str
    total_minor: int
    payment_id: str | None
    line_items: list[SaleLineItemOut]
    created_at: datetime
    updated_at: datetime


class PaymentRequestIn(BaseModel):
    phone: str = Field(pattern=PHONE_PATTERN)


class PaymentRequestOut(BaseModel):
    sale_id: str
    payment_id: str
    status: str


class PaymentEventIn(BaseModel):
    """Body Payments sends to POS's internal callback.

    ``event_id`` is Payments' own idempotency key for this specific event —
    distinct from ``payment_id``, since one payment can produce more than
    one event (e.g. a query/reconcile correction after a timeout).
    """

    event_id: str = Field(min_length=1, max_length=80)
    payment_id: str
    status: str
    amount_minor: int = Field(gt=0)
    occurred_at: datetime | None = None

    @field_validator("status")
    @classmethod
    def status_must_be_terminal(cls, v: str) -> str:
        if v not in {"PAID", "PAYMENT_FAILED"}:
            raise ValueError("status must be PAID or PAYMENT_FAILED")
        return v


class PaymentEventOut(BaseModel):
    sale_id: str
    status: str
    applied: bool
