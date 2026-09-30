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
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from . import spans, tracing


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
        with _client_span("POST /payments") as (trace_headers, result), httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base_url}/payments",
                json=payload,
                headers={"Idempotency-Key": idempotency_key, **trace_headers},
            )
            result["status"] = resp.status_code
            resp.raise_for_status()
            return resp.json()

    def get_payment(self, payment_id: str) -> dict:
        with _client_span("GET /payments/{payment_id}") as (trace_headers, result), httpx.Client(timeout=self.timeout) as client:
            resp = client.get(f"{self.base_url}/payments/{payment_id}", headers=trace_headers)
            result["status"] = resp.status_code
            resp.raise_for_status()
            return resp.json()


@contextmanager
def _client_span(route: str) -> Iterator[tuple[dict[str, str], dict[str, int]]]:
    """A client span for one call to Payments (X-Ray's POS -> Payments edge). Yields the headers
    carrying the sale's trace id with this span as parent, and a dict for the response status.
    Outside a request (the background reconcile loop) there is no trace: no header, no span."""
    request = tracing.current_request_span()
    span_id = spans.new_span_id()
    header = tracing.traceparent(span_id)
    result: dict[str, int] = {}
    start = spans.now_ns()
    try:
        yield ({"traceparent": header} if header else {}), result
    finally:
        if request is not None and header:
            status = result.get("status", 0)
            spans.record(
                trace_id=request.trace_id,
                span_id=span_id,
                parent_span_id=request.span_id,
                name=f"payments {route}",
                kind=spans.CLIENT,
                start_ns=start,
                end_ns=spans.now_ns(),
                attributes={"http.method": route.split()[0], "http.route": route, "http.status_code": status,
                            "peer.service": "payments"},
                error=status == 0 or status >= 500,
            )


def get_payments_client() -> PaymentsClient:
    return PaymentsClient()
