"""MpesaPort: the only way Payments and Commission-facing code reaches M-Pesa (ADR 0004).

Skeleton scope: STK collection path. Auth and B2C from ADR 0004 land with G2.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from mpesa.models import CallbackEvent, ChargeAccepted, ChargeRequest, PaymentStatus


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
