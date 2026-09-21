"""payment-reconcile is the active integration path with Payments (a poll of
GET /payments/{id}, not a push) — see app/routers/sales.py. These prove the
same invariants test_payment_events.py proves for the (currently unused)
push webhook: one legal transition, one ledger effect, safe under replay
and reordering, and "a timeout is not a decline".
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import FakePaymentsClient, create_ready_sale


def _request_payment(client: TestClient, tenant_id: str, sale_id: str) -> dict:
    resp = client.post(
        f"/tenants/{tenant_id}/sales/{sale_id}/payment-request", json={"phone": "254712345678"}
    )
    assert resp.status_code == 200
    return resp.json()


def test_reconcile_while_still_pending_is_a_safe_no_op(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    _request_payment(client, tenant_id, sale["id"])
    # FakePaymentsClient defaults a fresh payment to PENDING — no verdict yet.

    resp = client.post(f"/tenants/{tenant_id}/sales/{sale['id']}/payment-reconcile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PAYMENT_REQUESTED"  # unchanged — a timeout is not a decline
    assert body["applied"] is False
    assert body["payments_state"] == "PENDING"


def test_reconcile_applies_succeeded_as_paid(
    client: TestClient,
    tenant_id: str,
    till_id: str,
    attendant_id: str,
    product_id: str,
    fake_payments_client: FakePaymentsClient,
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    requested = _request_payment(client, tenant_id, sale["id"])
    fake_payments_client.set_state(requested["payment_id"], "SUCCEEDED", amount_minor=sale["total_minor"])

    resp = client.post(f"/tenants/{tenant_id}/sales/{sale['id']}/payment-reconcile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PAID"
    assert body["applied"] is True
    assert body["payments_state"] == "SUCCEEDED"


def test_repeated_reconcile_after_settling_is_a_no_op(
    client: TestClient,
    tenant_id: str,
    till_id: str,
    attendant_id: str,
    product_id: str,
    fake_payments_client: FakePaymentsClient,
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    requested = _request_payment(client, tenant_id, sale["id"])
    fake_payments_client.set_state(requested["payment_id"], "SUCCEEDED", amount_minor=sale["total_minor"])

    first = client.post(f"/tenants/{tenant_id}/sales/{sale['id']}/payment-reconcile")
    assert first.json()["applied"] is True

    # Cron/poller runs again before the next scheduled interval — same
    # (payment_id, state) pair, must not double-apply.
    second = client.post(f"/tenants/{tenant_id}/sales/{sale['id']}/payment-reconcile")
    assert second.status_code == 200
    assert second.json() == {
        "sale_id": sale["id"],
        "status": "PAID",
        "applied": False,
        "payments_state": "SUCCEEDED",
    }


def test_stale_declined_after_already_paid_does_not_override(
    client: TestClient,
    tenant_id: str,
    till_id: str,
    attendant_id: str,
    product_id: str,
    fake_payments_client: FakePaymentsClient,
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    requested = _request_payment(client, tenant_id, sale["id"])

    fake_payments_client.set_state(requested["payment_id"], "SUCCEEDED", amount_minor=sale["total_minor"])
    paid = client.post(f"/tenants/{tenant_id}/sales/{sale['id']}/payment-reconcile")
    assert paid.json()["status"] == "PAID"

    # A confused/late poll result reports DECLINED for a payment that has
    # already settled PAID — must be rejected, not flip the sale back.
    fake_payments_client.set_state(requested["payment_id"], "DECLINED", amount_minor=sale["total_minor"])
    resp = client.post(f"/tenants/{tenant_id}/sales/{sale['id']}/payment-reconcile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PAID"
    assert body["applied"] is False

    sale_after = client.get(f"/tenants/{tenant_id}/sales/{sale['id']}").json()
    assert sale_after["status"] == "PAID"


def test_reconcile_amount_mismatch_is_rejected(
    client: TestClient,
    tenant_id: str,
    till_id: str,
    attendant_id: str,
    product_id: str,
    fake_payments_client: FakePaymentsClient,
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    requested = _request_payment(client, tenant_id, sale["id"])
    fake_payments_client.set_state(
        requested["payment_id"], "SUCCEEDED", amount_minor=sale["total_minor"] + 1
    )

    resp = client.post(f"/tenants/{tenant_id}/sales/{sale['id']}/payment-reconcile")
    assert resp.status_code == 409

    sale_after = client.get(f"/tenants/{tenant_id}/sales/{sale['id']}").json()
    assert sale_after["status"] == "PAYMENT_REQUESTED"


def test_reconcile_before_payment_requested_is_conflict(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)

    resp = client.post(f"/tenants/{tenant_id}/sales/{sale['id']}/payment-reconcile")
    assert resp.status_code == 409


def test_payment_request_sends_msisdn_and_account_reference_to_payments(
    client: TestClient,
    tenant_id: str,
    till_id: str,
    attendant_id: str,
    product_id: str,
    fake_payments_client: FakePaymentsClient,
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    _request_payment(client, tenant_id, sale["id"])

    call = fake_payments_client.calls[0]
    assert call["msisdn"] == "254712345678"
    assert "phone" not in call
    assert call["account_reference"] == sale["id"][:12]
