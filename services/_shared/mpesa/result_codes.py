"""Provider ResultCode to normalised outcome (ADR 0006 section 3 mapping table).

Only codes verified against Daraja documentation may produce a terminal outcome. Verified
2026-09-21 against the M-Pesa Express and Express Query pages (callbacks and query responses):
0 (success) and 1032 (cancelled by the user). Everything else is UNKNOWN by design (fail safe,
never a decline), including the candidate codes 1, 2001 and 1037: they appear on neither page, so
they stay UNKNOWN (then reconcile or manual review) until verified from Daraja docs or a sandbox
contract test. Add a code to _TABLE only after that, and update ADR 0006 in the same change.

STK collection path only. Result codes are API-specific: the Daraja Reversals page gives codes 1 and
2001 different meanings, so never reuse this table for reversals, B2C or other APIs.
"""

from __future__ import annotations

from mpesa.models import DeclineReason, Outcome

RESULT_SUCCESS = 0
RESULT_USER_CANCELLED = 1032
# Candidate codes, UNVERIFIED and deliberately absent from _TABLE. The FakeAdapter emits them so
# tests prove they resolve to UNKNOWN. Intended meanings once verified: insufficient funds
# (DECLINED), wrong PIN (DECLINED), prompt not answered (EXPIRED).
RESULT_INSUFFICIENT_FUNDS = 1
RESULT_WRONG_PIN = 2001
RESULT_PROMPT_NOT_ANSWERED = 1037
# Deliberately outside the mapping table, used by the FakeAdapter to test fail-safe handling.
RESULT_FAKE_UNRECOGNISED = 99999

_TABLE = {
    RESULT_SUCCESS: (Outcome.SUCCEEDED, None),
    RESULT_USER_CANCELLED: (Outcome.DECLINED, DeclineReason.USER_CANCELLED),
}

VERIFIED_CODES: frozenset[int] = frozenset(_TABLE)


def classify(code: object) -> tuple[Outcome, DeclineReason | None]:
    """Map a raw ResultCode to (outcome, decline_reason). Unrecognised input is UNKNOWN."""
    if isinstance(code, bool) or not isinstance(code, int):
        return Outcome.UNKNOWN, None
    return _TABLE.get(code, (Outcome.UNKNOWN, None))
