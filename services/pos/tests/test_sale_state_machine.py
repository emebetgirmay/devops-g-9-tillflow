from __future__ import annotations

from fastapi.testclient import TestClient

from app.state import InvalidTransition, SaleStatus, transition
from tests.conftest import FakePaymentsClient, create_ready_sale


def test_transition_table_unit() -> None:
    assert transition(SaleStatus.READY_FOR_PAYMENT, SaleStatus.PAYMENT_REQUESTED) == SaleStatus.PAYMENT_REQUESTED
    assert transition(SaleStatus.PAYMENT_REQUESTED, SaleStatus.PAID) == SaleStatus.PAID

    # PAID has no outgoing edges at all — not even to itself. A second PAID
    # event for an already-paid sale is handled by the caller (see
    # test_payment_events.py), not by treating this as a legal transition.
    for target in (SaleStatus.PAID, SaleStatus.PAYMENT_FAILED, SaleStatus.VOID, SaleStatus.READY_FOR_PAYMENT):
        try:
            transition(SaleStatus.PAID, target)
        except InvalidTransition:
            continue
        else:
            raise AssertionError(f"PAID must be terminal, but transition to {target} was allowed")


def test_payment_request_moves_sale_to_payment_requested(
    client: TestClient,
    tenant_id: str,
    till_id: str,
    attendant_id: str,
    product_id: str,
    fake_payments_client: FakePaymentsClient,
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)

    resp = client.post(
        f"/tenants/{tenant_id}/sales/{sale['id']}/payment-request",
        json={"phone": "254712345678"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "PAYMENT_REQUESTED"
    assert body["payment_id"]

    # POS told Payments exactly the sale's server-computed total, and used
    # the sale_id as the payment idempotency key.
    assert len(fake_payments_client.calls) == 1
    call = fake_payments_client.calls[0]
    assert call["sale_id"] == sale["id"]
    assert call["amount_minor"] == sale["total_minor"]
    assert call["idempotency_key"] == sale["id"]


def test_cannot_request_payment_twice_while_in_flight(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)

    first = client.post(
        f"/tenants/{tenant_id}/sales/{sale['id']}/payment-request", json={"phone": "254712345678"}
    )
    assert first.status_code == 200

    second = client.post(
        f"/tenants/{tenant_id}/sales/{sale['id']}/payment-request", json={"phone": "254712345678"}
    )
    assert second.status_code == 409


def test_payment_request_for_unknown_sale_is_404(client: TestClient, tenant_id: str) -> None:
    resp = client.post(
        f"/tenants/{tenant_id}/sales/does-not-exist/payment-request", json={"phone": "254712345678"}
    )
    assert resp.status_code == 404
