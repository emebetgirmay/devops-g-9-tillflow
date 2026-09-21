"""Typed request/response models for the M-Pesa port (ADR 0004, ADR 0006)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# ADR 0006 section 2: 16 to 64 characters, [A-Za-z0-9_-].
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# Kenyan MSISDN in international form, digits only.
MSISDN_RE = re.compile(r"^254[0-9]{9}$")


class Outcome(str, Enum):
    """Normalised provider outcome. UNKNOWN means no definitive answer (never a decline)."""

    SUCCEEDED = "SUCCEEDED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class DeclineReason(str, Enum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    USER_CANCELLED = "USER_CANCELLED"
    WRONG_PIN = "WRONG_PIN"
    REJECTED_AT_INITIATION = "REJECTED_AT_INITIATION"


@dataclass(frozen=True)
class ChargeRequest:
    """Request to start an STK-style charge. Amounts are integer minor units."""

    idempotency_key: str
    tenant_id: str
    msisdn: str
    amount_minor: int
    currency: str = "KES"
    reference: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not IDEMPOTENCY_KEY_RE.match(self.idempotency_key):
            raise ValueError("idempotency_key must be 16-64 chars of [A-Za-z0-9_-]")
        if not MSISDN_RE.match(self.msisdn):
            raise ValueError("msisdn must be 254 followed by 9 digits")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be an int (minor units)")
        if self.amount_minor <= 0:
            raise ValueError("amount_minor must be positive")


@dataclass(frozen=True)
class ChargeAccepted:
    """Provider accepted the initiate call. provider_ref is the dedupe key (ADR 0006)."""

    provider_ref: str
    merchant_request_id: str


@dataclass(frozen=True)
class PaymentStatus:
    """Result of a status query. outcome UNKNOWN means still undetermined."""

    provider_ref: str
    outcome: Outcome
    decline_reason: DeclineReason | None = None
    receipt: str | None = None
    raw_code: str | None = None


@dataclass(frozen=True)
class CallbackEvent:
    """A verified, parsed provider callback, ready for the Commission/Payments handler."""

    provider_ref: str
    outcome: Outcome
    decline_reason: DeclineReason | None
    receipt: str | None
    amount_minor: int | None
    raw_code: str
    payload_sha256: str
