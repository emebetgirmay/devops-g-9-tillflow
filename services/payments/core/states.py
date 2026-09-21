"""State machines and the only legal transitions (ADR 0006 section 1, ADR 0008 section 4).

Terminal states are immutable. Anything not in the tables is illegal: it is rejected and logged,
never applied. The service updates a state only through Store.transition, which calls
check_transition first.
"""

from __future__ import annotations

from enum import Enum


class PaymentState(str, Enum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    SUCCEEDED = "SUCCEEDED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"


class DisbursementState(str, Enum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


PAYMENT_TERMINAL = frozenset({PaymentState.SUCCEEDED, PaymentState.DECLINED, PaymentState.EXPIRED})
DISBURSEMENT_TERMINAL = frozenset({DisbursementState.SUCCEEDED, DisbursementState.FAILED})

P = PaymentState
PAYMENT_LEGAL: frozenset[tuple[PaymentState, PaymentState]] = frozenset(
    {
        (P.CREATED, P.PENDING),
        (P.CREATED, P.DECLINED),
        (P.CREATED, P.UNKNOWN),
        (P.PENDING, P.SUCCEEDED),
        (P.PENDING, P.DECLINED),
        (P.PENDING, P.EXPIRED),
        (P.PENDING, P.UNKNOWN),
        (P.UNKNOWN, P.SUCCEEDED),
        (P.UNKNOWN, P.DECLINED),
        (P.UNKNOWN, P.EXPIRED),
        (P.UNKNOWN, P.NEEDS_REVIEW),
        (P.NEEDS_REVIEW, P.SUCCEEDED),
        (P.NEEDS_REVIEW, P.DECLINED),
        (P.NEEDS_REVIEW, P.EXPIRED),
    }
)

D = DisbursementState
DISBURSEMENT_LEGAL: frozenset[tuple[DisbursementState, DisbursementState]] = frozenset(
    {
        (D.CREATED, D.PENDING),
        (D.CREATED, D.FAILED),
        (D.CREATED, D.UNKNOWN),
        (D.PENDING, D.SUCCEEDED),
        (D.PENDING, D.FAILED),
        (D.PENDING, D.UNKNOWN),
        (D.UNKNOWN, D.SUCCEEDED),
        (D.UNKNOWN, D.FAILED),
        (D.UNKNOWN, D.NEEDS_REVIEW),
        (D.NEEDS_REVIEW, D.SUCCEEDED),
        (D.NEEDS_REVIEW, D.FAILED),
    }
)


class IllegalTransition(Exception):
    """A state change that the machine does not allow."""


def check_transition(kind: str, current: Enum, target: Enum) -> None:
    legal = PAYMENT_LEGAL if kind == "payment" else DISBURSEMENT_LEGAL
    if (current, target) not in legal:
        raise IllegalTransition(f"{kind}: {current.value} -> {target.value} is not allowed")


def is_terminal(kind: str, state: Enum) -> bool:
    terminal = PAYMENT_TERMINAL if kind == "payment" else DISBURSEMENT_TERMINAL
    return state in terminal
