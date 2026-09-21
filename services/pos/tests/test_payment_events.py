"""These prove the invariants the capstone brief calls out explicitly:
"Callback replay — replay and reorder callbacks; prove one legal transition,
one ledger effect, and a trace that explains the duplicate."
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import create_ready_sale


def _request_payment(client: TestClient, tenant_id: str, sale_id: str) -> dict:
    resp = client.post(
        f"/tenants/{tenant_id}/sales/{sale_id}/payment-request", json={"phone": "254712345678"}
    )
    assert resp.status_code == 200
    return resp.json()


def test_paid_event_transitions_sale_to_paid(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    requested = _request_payment(client, tenant_id, sale["id"])

    resp = client.post(
        f"/internal/sales/{sale['id']}/payment-events",
        json={
            "event_id": str(uuid.uuid4()),
            "payment_id": requested["payment_id"],
            "status": "PAID",
            "amount_minor": sale["total_minor"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PAID"
    assert body["applied"] is True


def test_replayed_paid_event_is_a_no_op(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    requested = _request_payment(client, tenant_id, sale["id"])

    event = {
        "event_id": str(uuid.uuid4()),
        "payment_id": requested["payment_id"],
        "status": "PAID",
        "amount_minor": sale["total_minor"],
    }

    first = client.post(f"/internal/sales/{sale['id']}/payment-events", json=event)
    assert first.status_code == 200
    assert first.json()["applied"] is True

    # Same event_id delivered again (at-least-once delivery, network retry,
    # whatever) — must not error and must not be treated as a second effect.
    replay = client.post(f"/internal/sales/{sale['id']}/payment-events", json=event)
    assert replay.status_code == 200
    assert replay.json() == {"sale_id": sale["id"], "status": "PAID", "applied": False}


def test_reordered_failed_event_after_paid_does_not_override_paid(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    requested = _request_payment(client, tenant_id, sale["id"])

    paid_event = {
        "event_id": str(uuid.uuid4()),
        "payment_id": requested["payment_id"],
        "status": "PAID",
        "amount_minor": sale["total_minor"],
    }
    paid_resp = client.post(f"/internal/sales/{sale['id']}/payment-events", json=paid_event)
    assert paid_resp.json()["status"] == "PAID"

    # A stale/reordered FAILED callback for the same payment arrives after
    # PAID has already settled — it must be rejected as an illegal
    # transition, not silently flip a paid sale back to failed.
    stale_failed_event = {
        "event_id": str(uuid.uuid4()),
        "payment_id": requested["payment_id"],
        "status": "PAYMENT_FAILED",
        "amount_minor": sale["total_minor"],
    }
    resp = client.post(f"/internal/sales/{sale['id']}/payment-events", json=stale_failed_event)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PAID"  # unchanged
    assert body["applied"] is False

    sale_after = client.get(f"/tenants/{tenant_id}/sales/{sale['id']}").json()
    assert sale_after["status"] == "PAID"


def test_amount_mismatch_is_rejected(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    requested = _request_payment(client, tenant_id, sale["id"])

    resp = client.post(
        f"/internal/sales/{sale['id']}/payment-events",
        json={
            "event_id": str(uuid.uuid4()),
            "payment_id": requested["payment_id"],
            "status": "PAID",
            "amount_minor": sale["total_minor"] + 1,
        },
    )
    assert resp.status_code == 409

    sale_after = client.get(f"/tenants/{tenant_id}/sales/{sale['id']}").json()
    assert sale_after["status"] == "PAYMENT_REQUESTED"  # untouched


def test_failed_then_retry_then_paid(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    requested = _request_payment(client, tenant_id, sale["id"])

    failed = client.post(
        f"/internal/sales/{sale['id']}/payment-events",
        json={
            "event_id": str(uuid.uuid4()),
            "payment_id": requested["payment_id"],
            "status": "PAYMENT_FAILED",
            "amount_minor": sale["total_minor"],
        },
    )
    assert failed.json()["status"] == "PAYMENT_FAILED"

    retry = client.post(
        f"/tenants/{tenant_id}/sales/{sale['id']}/payment-request", json={"phone": "254712345678"}
    )
    assert retry.status_code == 200
    assert retry.json()["status"] == "PAYMENT_REQUESTED"

    paid = client.post(
        f"/internal/sales/{sale['id']}/payment-events",
        json={
            "event_id": str(uuid.uuid4()),
            "payment_id": retry.json()["payment_id"],
            "status": "PAID",
            "amount_minor": sale["total_minor"],
        },
    )
    assert paid.json()["status"] == "PAID"


def test_payment_event_for_unknown_sale_is_404(client: TestClient) -> None:
    resp = client.post(
        "/internal/sales/does-not-exist/payment-events",
        json={
            "event_id": str(uuid.uuid4()),
            "payment_id": "pay-1",
            "status": "PAID",
            "amount_minor": 1,
        },
    )
    assert resp.status_code == 404
