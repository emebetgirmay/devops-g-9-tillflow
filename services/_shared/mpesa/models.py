"""Typed request/response models for the M-Pesa port (ADR 0004, ADR 0006)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# ADR 0006 section 2: 16 to 64 characters, [A-Za-z0-9_-].
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# Kenyan MSISDN in international form, digits only.
MSISDN_RE = re.compile(r"^254[0-9]{9}$")
# Documented STK Push request limits (verified 2026-09-21).
MAX_REFERENCE_LEN = 12
MAX_DESCRIPTION_LEN = 13


class Outcome(str, Enum):
    """Normalised provider outcome. UNKNOWN means no definitive answer (never a decline).

    FAILED is used only for disbursements (B2C): a definitive failure, no money moved.
    """

    SUCCEEDED = "SUCCEEDED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class DeclineReason(str, Enum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    USER_CANCELLED = "USER_CANCELLED"
    WRONG_PIN = "WRONG_PIN"
    REJECTED_AT_INITIATION = "REJECTED_AT_INITIATION"


class FailureReason(str, Enum):
    """Why a disbursement definitively failed (ADR 0008 section 5)."""

    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    AMOUNT_TOO_LOW = "AMOUNT_TOO_LOW"
    AMOUNT_TOO_HIGH = "AMOUNT_TOO_HIGH"
    RECIPIENT_LIMIT = "RECIPIENT_LIMIT"
    RECIPIENT_NOT_REGISTERED = "RECIPIENT_NOT_REGISTERED"
    RECIPIENT_INVALID = "RECIPIENT_INVALID"
    ACCOUNT_STATE = "ACCOUNT_STATE"
    CONFIGURATION = "CONFIGURATION"
    REJECTED_AT_INITIATION = "REJECTED_AT_INITIATION"


@dataclass(frozen=True)
class ChargeRequest:
    """Request to start an STK-style charge. Amounts are integer minor units."""

    idempotency_key: str
    tenant_id: str
    msisdn: str
    amount_minor: int
    currency: str = "KES"
    reference: str = ""  # AccountReference: at most 12 characters (Daraja STK Push page)
    description: str = ""  # TransactionDesc: at most 13 characters (Daraja STK Push page)

    def __post_init__(self) -> None:
        if not IDEMPOTENCY_KEY_RE.match(self.idempotency_key):
            raise ValueError("idempotency_key must be 16-64 chars of [A-Za-z0-9_-]")
        if not MSISDN_RE.match(self.msisdn):
            raise ValueError("msisdn must be 254 followed by 9 digits")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be an int (minor units)")
        if self.amount_minor <= 0:
            raise ValueError("amount_minor must be positive")
        if len(self.reference) > MAX_REFERENCE_LEN:
            raise ValueError(f"reference must be at most {MAX_REFERENCE_LEN} characters")
        if len(self.description) > MAX_DESCRIPTION_LEN:
            raise ValueError(f"description must be at most {MAX_DESCRIPTION_LEN} characters")


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


# B2C provider limits, from the Daraja B2C FAQ (verified 2026-09-21): minimum KSh 10, maximum
# KSh 250,000 per transaction. Amounts here are minor units (cents).
MIN_DISBURSEMENT_MINOR = 1_000
MAX_DISBURSEMENT_MINOR = 25_000_000
ORIGINATOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


@dataclass(frozen=True)
class DisbursementRequest:
    """Request to pay a recipient via B2C. originator_conversation_id is chosen by the caller,
    derived deterministically from the disbursement, so a repeat is rejected by the provider."""

    idempotency_key: str
    tenant_id: str
    originator_conversation_id: str
    msisdn: str
    amount_minor: int
    currency: str = "KES"
    remarks: str = "commission"

    def __post_init__(self) -> None:
        if not IDEMPOTENCY_KEY_RE.match(self.idempotency_key):
            raise ValueError("idempotency_key must be 16-64 chars of [A-Za-z0-9_-]")
        if not ORIGINATOR_ID_RE.match(self.originator_conversation_id):
            raise ValueError("originator_conversation_id must be 1-20 chars of [A-Za-z0-9_-]")
        if not MSISDN_RE.match(self.msisdn):
            raise ValueError("msisdn must be 254 followed by 9 digits")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be an int (minor units)")
        if self.amount_minor <= 0:
            raise ValueError("amount_minor must be positive")
        if not 2 <= len(self.remarks) <= 100:
            raise ValueError("remarks must be 2 to 100 characters")


@dataclass(frozen=True)
class DisbursementAccepted:
    """Provider accepted the request. The originator id is ours; conversation_id is theirs."""

    originator_conversation_id: str
    conversation_id: str


@dataclass(frozen=True)
class DisbursementStatus:
    """Result of a status query. outcome UNKNOWN means still undetermined."""

    originator_conversation_id: str
    outcome: Outcome
    failure_reason: FailureReason | None = None
    receipt: str | None = None
    raw_code: str | None = None


@dataclass(frozen=True)
class DisbursementEvent:
    """A verified, parsed B2C result callback."""

    originator_conversation_id: str
    conversation_id: str | None
    outcome: Outcome
    failure_reason: FailureReason | None
    receipt: str | None
    amount_minor: int | None
    raw_code: str
    payload_sha256: str
