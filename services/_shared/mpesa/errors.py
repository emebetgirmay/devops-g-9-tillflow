"""Port errors. Definitive declines and unknown outcomes are different types (ADR 0006)."""

from __future__ import annotations

from mpesa.models import DeclineReason


class MpesaError(Exception):
    """Base class for port errors."""


class ChargeDeclinedError(MpesaError):
    """The provider definitively refused the initiate call. No charge exists."""

    def __init__(self, reason: DeclineReason, raw_code: str | None = None) -> None:
        super().__init__(f"charge declined: {reason.value}")
        self.reason = reason
        self.raw_code = raw_code


class OutcomeUnknownError(MpesaError):
    """Timeout or transport failure: the outcome is unknown. Never treat as a decline,
    never retry the initiate call (ADR 0006 section 3)."""


class UnknownReferenceError(MpesaError):
    """A status query named a provider reference the provider does not know."""


class CallbackAuthenticityError(MpesaError):
    """The callback failed the authenticity check. Nothing may be trusted from the body."""


class CallbackMalformedError(MpesaError):
    """The callback was authentic but its body could not be parsed."""
