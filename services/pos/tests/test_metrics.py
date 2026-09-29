"""ADR 0010 work item P-1: POS's own /metrics endpoint.

These check the contract that matters for Grafana/alerting, not just that
the endpoint returns 200: the exact metric names exist, labels use route
*templates* (never a resolved path with a real id in it), operational
routes are excluded, and no tenant/sale/attendant id ever leaks into a
label.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import FakePaymentsClient, create_ready_sale


def test_metrics_endpoint_returns_prometheus_text(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    for name in (
        "pos_http_requests_total",
        "pos_http_request_duration_seconds",
        "pos_sale_creates_total",
        "pos_payment_events_total",
        "pos_sales_paid_total",
    ):
        assert name in body


def test_health_ready_version_metrics_excluded(client: TestClient) -> None:
    client.get("/health")
    client.get("/version")
    body = client.get("/metrics").text
    assert 'route="/health"' not in body
    assert 'route="/version"' not in body
    assert 'route="/metrics"' not in body


def test_route_label_is_a_template_not_a_resolved_path(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    # Two different tenant_ids hitting "the same" endpoint must collapse
    # into one route label, not one label per tenant_id (that's exactly the
    # unbounded-cardinality outage ADR 0010 warns about).
    other_tenant = client.post("/tenants", json={"name": "Other Duka"}).json()["id"]
    client.get(f"/tenants/{tenant_id}/sales/does-not-exist")
    client.get(f"/tenants/{other_tenant}/sales/does-not-exist")

    body = client.get("/metrics").text
    assert 'route="/tenants/{tenant_id}/sales/{sale_id}"' in body
    assert tenant_id not in body
    assert other_tenant not in body


def test_unmatched_path_is_bucketed_not_leaked(client: TestClient) -> None:
    secret_looking_path = f"/this-does-not-exist/{uuid.uuid4()}"
    client.get(secret_looking_path)
    body = client.get("/metrics").text
    assert 'route="unmatched"' in body
    assert secret_looking_path not in body


def test_sale_create_metrics_by_result(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    key = str(uuid.uuid4())
    body = {
        "till_id": till_id,
        "attendant_id": attendant_id,
        "line_items": [{"product_id": product_id, "quantity": 1}],
    }

    created = client.post(f"/tenants/{tenant_id}/sales", headers={"Idempotency-Key": key}, json=body)
    assert created.status_code == 201

    replayed = client.post(f"/tenants/{tenant_id}/sales", headers={"Idempotency-Key": key}, json=body)
    assert replayed.status_code == 201

    conflicting_body = {**body, "line_items": [{"product_id": product_id, "quantity": 99}]}
    conflict = client.post(
        f"/tenants/{tenant_id}/sales", headers={"Idempotency-Key": key}, json=conflicting_body
    )
    assert conflict.status_code == 409

    invalid = client.post(
        f"/tenants/{tenant_id}/sales",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"till_id": "does-not-exist", "attendant_id": attendant_id, "line_items": body["line_items"]},
    )
    assert invalid.status_code == 400

    metrics_body = client.get("/metrics").text
    assert 'pos_sale_creates_total{result="created"} 1.0' in metrics_body
    assert 'pos_sale_creates_total{result="replayed"} 1.0' in metrics_body
    assert 'pos_sale_creates_total{result="conflict"} 1.0' in metrics_body
    assert 'pos_sale_creates_total{result="invalid"} 1.0' in metrics_body


def test_payment_event_and_paid_metrics(
    client: TestClient,
    tenant_id: str,
    till_id: str,
    attendant_id: str,
    product_id: str,
    fake_payments_client: FakePaymentsClient,
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    req = client.post(
        f"/tenants/{tenant_id}/sales/{sale['id']}/payment-request", json={"phone": "254712345678"}
    ).json()
    fake_payments_client.set_state(req["payment_id"], "SUCCEEDED", amount_minor=sale["total_minor"])

    applied = client.post(f"/tenants/{tenant_id}/sales/{sale['id']}/payment-reconcile")
    assert applied.json()["applied"] is True

    replay = client.post(f"/tenants/{tenant_id}/sales/{sale['id']}/payment-reconcile")
    assert replay.json()["applied"] is False

    metrics_body = client.get("/metrics").text
    assert 'pos_payment_events_total{result="applied"} 1.0' in metrics_body
    assert 'pos_payment_events_total{result="replay"} 1.0' in metrics_body
    assert "pos_sales_paid_total 1.0" in metrics_body


def test_no_ids_ever_leak_into_metric_labels(
    client: TestClient,
    tenant_id: str,
    till_id: str,
    attendant_id: str,
    product_id: str,
    fake_payments_client: FakePaymentsClient,
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    client.post(f"/tenants/{tenant_id}/sales/{sale['id']}/payment-request", json={"phone": "254712345678"})

    body = client.get("/metrics").text
    assert tenant_id not in body
    assert sale["id"] not in body
    assert till_id not in body
    assert attendant_id not in body
    assert product_id not in body
