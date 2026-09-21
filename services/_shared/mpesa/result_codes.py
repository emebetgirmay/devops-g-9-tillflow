"""Provider ResultCode to normalised outcome (ADR 0006 section 3 mapping table).

Verified 2026-09-21 against the Daraja M-Pesa Express and Express Query pages (callbacks and
query responses): 0 (success) and 1032 (cancelled by the user). Codes 1, 2001 and 1037 appear on
neither page and are still UNVERIFIED: verify against Daraja docs before G2 relies on them.
Anything not listed is UNKNOWN by design (fail safe, never a decline).
"""

from __future__ import annotations

from mpesa.models import DeclineReason, Outcome

RESULT_SUCCESS = 0
RESULT_INSUFFICIENT_FUNDS = 1
RESULT_USER_CANCELLED = 1032
RESULT_WRONG_PIN = 2001
RESULT_PROMPT_NOT_ANSWERED = 1037
# Deliberately outside the mapping table, used by the FakeAdapter to test fail-safe handling.
RESULT_FAKE_UNRECOGNISED = 99999

_TABLE = {
    RESULT_SUCCESS: (Outcome.SUCCEEDED, None),
    RESULT_INSUFFICIENT_FUNDS: (Outcome.DECLINED, DeclineReason.INSUFFICIENT_FUNDS),
    RESULT_USER_CANCELLED: (Outcome.DECLINED, DeclineReason.USER_CANCELLED),
    RESULT_WRONG_PIN: (Outcome.DECLINED, DeclineReason.WRONG_PIN),
    RESULT_PROMPT_NOT_ANSWERED: (Outcome.EXPIRED, None),
}


def classify(code: object) -> tuple[Outcome, DeclineReason | None]:
    """Map a raw ResultCode to (outcome, decline_reason). Unrecognised input is UNKNOWN."""
    if isinstance(code, bool) or not isinstance(code, int):
        return Outcome.UNKNOWN, None
    return _TABLE.get(code, (Outcome.UNKNOWN, None))
