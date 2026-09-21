"""M-Pesa ports: the only way Payments code reaches M-Pesa (ADR 0004).

MpesaPort is the STK collection path. DisbursementPort is the B2C payout path (ADR 0008).
Commission never imports either: it calls the Payments API only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from mpesa.models import (
    CallbackEvent,
    ChargeAccepted,
    ChargeRequest,
    DisbursementAccepted,
    DisbursementEvent,
    DisbursementRequest,
    DisbursementStatus,
    PaymentStatus,
)


@runtime_checkable
class MpesaPort(Protocol):
    def initiate_charge(self, request: ChargeRequest) -> ChargeAccepted:
        """Start a charge. Callers must call this at most once per idempotency key.

        Raises ChargeDeclinedError if the provider definitively refused (no charge exists).
        Raises OutcomeUnknownError on timeout or transport failure (a charge may exist);
        the caller must not retry, only reconcile via query_status or a late callback.
        """
        ...

    def query_status(self, provider_ref: str) -> PaymentStatus:
        """Ask the provider for the state of a charge. Outcome UNKNOWN means undetermined.

        Raises OutcomeUnknownError if the query itself timed out.
        Raises UnknownReferenceError if the provider does not know the reference.
        """
        ...

    def parse_callback(self, headers: Mapping[str, str], body: bytes) -> CallbackEvent:
        """Verify authenticity, then parse a provider callback into a normalised event.

        Raises CallbackAuthenticityError before parsing if verification fails.
        Raises CallbackMalformedError if the body is authentic but unparseable.
        """
        ...


@runtime_checkable
class DisbursementPort(Protocol):
    def disburse(self, request: DisbursementRequest) -> DisbursementAccepted:
        """Send a B2C payment. The caller-chosen originator_conversation_id makes a repeat
        detectable: the provider rejects a duplicate.

        Raises DisbursementRejectedError if the provider definitively refused (no money moved).
        Raises DuplicateOriginatorConversationError if the id was already used (outcome unknown:
        reconcile by that id). Raises OutcomeUnknownError on timeout or transport failure (money
        may have moved); the caller must never resubmit, only reconcile.
        """
        ...

    def query_disbursement_status(self, originator_conversation_id: str) -> DisbursementStatus:
        """Ask the provider for the state of a disbursement (reconciliation). Outcome UNKNOWN
        means undetermined. Raises OutcomeUnknownError if the query itself timed out and
        UnknownReferenceError if the provider does not know the id."""
        ...

    def parse_disbursement_result(
        self, headers: Mapping[str, str], body: bytes
    ) -> DisbursementEvent:
        """Verify authenticity, then parse a B2C result callback into a normalised event.

        Raises CallbackAuthenticityError before parsing if verification fails and
        CallbackMalformedError if the body is authentic but unparseable.
        """
        ...
