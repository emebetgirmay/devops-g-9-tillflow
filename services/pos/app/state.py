"""Sale status enum and the legal transition table.

A timeout/failure is never silently mapped onto another status, and a
transition that isn't in ``TRANSITIONS`` raises rather than being applied —
that's what makes replayed/reordered payment callbacks safe to handle: the
same event applied twice is a no-op, a contradicting event after a terminal
state is rejected (and only recorded for audit), never overwritten.
"""

from __future__ import annotations

from enum import Enum


class SaleStatus(str, Enum):
    READY_FOR_PAYMENT = "READY_FOR_PAYMENT"
    PAYMENT_REQUESTED = "PAYMENT_REQUESTED"
    PAID = "PAID"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    VOID = "VOID"


TERMINAL_STATUSES = {SaleStatus.PAID, SaleStatus.VOID}

TRANSITIONS: dict[SaleStatus, set[SaleStatus]] = {
    SaleStatus.READY_FOR_PAYMENT: {SaleStatus.PAYMENT_REQUESTED, SaleStatus.VOID},
    SaleStatus.PAYMENT_REQUESTED: {SaleStatus.PAID, SaleStatus.PAYMENT_FAILED},
    SaleStatus.PAYMENT_FAILED: {SaleStatus.PAYMENT_REQUESTED, SaleStatus.VOID},
    SaleStatus.PAID: set(),
    SaleStatus.VOID: set(),
}


class InvalidTransition(Exception):
    def __init__(self, current: SaleStatus, target: SaleStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"cannot transition sale from {current.value} to {target.value}")


def transition(current: SaleStatus, target: SaleStatus) -> SaleStatus:
    """Return the new status, or raise InvalidTransition.

    There is no same-state shortcut: ``PAYMENT_REQUESTED -> PAYMENT_REQUESTED``
    must raise (a second payment-request while one is in flight is a real
    conflict, not a no-op), and so must ``PAID -> PAID`` (PAID has no
    outgoing edges — it's terminal). Callers that need replay-safe handling
    of a *repeated event* (as opposed to a repeated action) — e.g. the same
    payment-confirmed callback delivered twice — catch InvalidTransition
    themselves and treat it as "no new effect", which is exactly the
    behavior a replayed or reordered callback needs.
    """
    if target not in TRANSITIONS.get(current, set()):
        raise InvalidTransition(current, target)
    return target
