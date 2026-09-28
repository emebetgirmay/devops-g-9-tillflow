"""B2C ResultCode to normalised outcome (ADR 0008 section 5).

Verified 2026-09-21 against the Daraja B2C page ("Result Codes" table and the failure sample).
B2C only: result codes are API-specific, so never share this table with STK collection (where
code 2001 means something else) or with reversals. Anything not listed is UNKNOWN by design.
The page types ResultCode as a string but its samples use numbers, so both are accepted.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

from mpesa.errors import CallbackMalformedError
from mpesa.models import DisbursementEvent, FailureReason, Outcome

_TABLE: dict[int | str, tuple[Outcome, FailureReason | None]] = {
    0: (Outcome.SUCCEEDED, None),
    1: (Outcome.FAILED, FailureReason.INSUFFICIENT_FUNDS),
    2: (Outcome.FAILED, FailureReason.AMOUNT_TOO_LOW),
    3: (Outcome.FAILED, FailureReason.AMOUNT_TOO_HIGH),
    4: (Outcome.FAILED, FailureReason.RECIPIENT_LIMIT),
    8: (Outcome.FAILED, FailureReason.RECIPIENT_LIMIT),
    11: (Outcome.FAILED, FailureReason.ACCOUNT_STATE),
    21: (Outcome.FAILED, FailureReason.CONFIGURATION),
    2001: (Outcome.FAILED, FailureReason.CONFIGURATION),
    2006: (Outcome.FAILED, FailureReason.ACCOUNT_STATE),
    2028: (Outcome.FAILED, FailureReason.CONFIGURATION),
    2040: (Outcome.FAILED, FailureReason.RECIPIENT_NOT_REGISTERED),
    8006: (Outcome.FAILED, FailureReason.CONFIGURATION),
    "SFC_IC0003": (Outcome.FAILED, FailureReason.RECIPIENT_INVALID),
}

VERIFIED_B2C_CODES: frozenset[int | str] = frozenset(_TABLE)


def normalise(code: object) -> int | str | None:
    """Return an int for numeric codes (int or digit string), a str for other strings, else None."""
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str):
        stripped = code.strip()
        return int(stripped) if stripped.isdigit() else stripped
    return None


def classify_b2c(code: object) -> tuple[Outcome, FailureReason | None]:
    """Map a raw B2C ResultCode to (outcome, failure_reason). Unrecognised input is UNKNOWN."""
    normalised = normalise(code)
    if normalised is None:
        return Outcome.UNKNOWN, None
    return _TABLE.get(normalised, (Outcome.UNKNOWN, None))


def parse_result(body: bytes) -> DisbursementEvent:
    """Parse a documented B2C result body (``Result``, ``ResultParameters`` only on success).

    No authenticity check here: callers verify the source first. Reads only our ids, the code,
    the receipt and the amount; the recipient's name and the account balances are never read.
    """
    try:
        result = json.loads(body)["Result"]
        oid = str(result["OriginatorConversationID"])
        raw_code = result["ResultCode"]
        conversation_id = result.get("ConversationID")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CallbackMalformedError("unparseable result body") from exc

    outcome, reason = classify_b2c(raw_code)
    receipt: str | None = None
    amount_minor: int | None = None
    try:
        params = result.get("ResultParameters", {}).get("ResultParameter", [])
        values = {item["Key"]: item.get("Value") for item in params}
        if "TransactionReceipt" in values:
            receipt = str(values["TransactionReceipt"])
        if "TransactionAmount" in values:
            amount_minor = int(Decimal(str(values["TransactionAmount"])) * 100)
    except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise CallbackMalformedError("unparseable result parameters") from exc
    if outcome is Outcome.SUCCEEDED and (receipt is None or amount_minor is None):
        raise CallbackMalformedError("success result missing receipt or amount")

    return DisbursementEvent(
        originator_conversation_id=oid,
        conversation_id=None if conversation_id is None else str(conversation_id),
        outcome=outcome,
        failure_reason=reason,
        receipt=receipt,
        amount_minor=amount_minor,
        raw_code=str(raw_code),
        payload_sha256=hashlib.sha256(body).hexdigest(),
    )
