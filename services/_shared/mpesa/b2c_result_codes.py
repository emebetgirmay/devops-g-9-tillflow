"""B2C ResultCode to normalised outcome (ADR 0008 section 5).

Verified 2026-09-21 against the Daraja B2C page ("Result Codes" table and the failure sample).
B2C only: result codes are API-specific, so never share this table with STK collection (where
code 2001 means something else) or with reversals. Anything not listed is UNKNOWN by design.
The page types ResultCode as a string but its samples use numbers, so both are accepted.
"""

from __future__ import annotations

from mpesa.models import FailureReason, Outcome

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
