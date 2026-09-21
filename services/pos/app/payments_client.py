"""Outbound client POS uses to talk to the Payments API (services/payments).

Field names and the Idempotency-Key-as-header convention here match Joy's
actual implementation (services/payments/core/payments.py), not the
draft in services/_shared/pos-payments-contract.md — that doc is stale on
the wire format and needs an update; the integration model itself (pull:
POS calls GET /payments/{id} to find out how a payment settled, there is
no push callback from Payments to POS) is correct and is what
app/routers/sales.py's payment-reconcile endpoint uses.

Tests override ``get_payments_client`` with a fake so sale/payment-request
tests never hit the network; that's also how a Payments outage should be
simulated for the G4 recovery drills.
"""

from __future__ import annotations

import os

import httpx


class PaymentsClient:
    def __init__(self, base_url: str | None = None, timeout: float = 5.0) -> None:
        self.base_url = (base_url or os.environ.get("PAYMENTS_BASE_URL", "http://payments.internal:8080")).rstrip("/")
        self.timeout = timeout

    def request_payment(
        self,
        *,
        sale_id: str,
        tenant_id: str,
        amount_minor: int,
        currency: str,
        msisdn: str,
        idempotency_key: str,
        account_reference: str = "",
    ) -> dict:
        payload = {
            "tenant_id": tenant_id,
            "sale_id": sale_id,
            "msisdn": msisdn,
            "amount": amount_minor,
            "currency": currency,
            "account_reference": account_reference,
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base_url}/payments",
                json=payload,
                headers={"Idempotency-Key": idempotency_key},
            )
            resp.raise_for_status()
            return resp.json()

    def get_payment(self, payment_id: str) -> dict:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(f"{self.base_url}/payments/{payment_id}")
            resp.raise_for_status()
            return resp.json()


def get_payments_client() -> PaymentsClient:
    return PaymentsClient()
