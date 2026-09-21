"""Outbound client POS uses to ask the Payments API for an STK push.

This is a plain internal HTTP call, not the Daraja adapter (that boundary
belongs to Payments — see docs/adrs/0004-mpesa-adapter.md). Tests override
``get_payments_client`` with a fake so sale/payment-request tests never hit
the network; that's also how a Payments outage should be simulated for the
G4 recovery drills.
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
        phone: str,
        idempotency_key: str,
    ) -> dict:
        payload = {
            "sale_id": sale_id,
            "tenant_id": tenant_id,
            "amount_minor": amount_minor,
            "currency": currency,
            "phone": phone,
            "idempotency_key": idempotency_key,
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}/payments", json=payload)
            resp.raise_for_status()
            return resp.json()


def get_payments_client() -> PaymentsClient:
    return PaymentsClient()
