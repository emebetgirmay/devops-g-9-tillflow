from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def test_create_sale_computes_total_from_catalog_price(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    resp = client.post(
        f"/tenants/{tenant_id}/sales",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "till_id": till_id,
            "attendant_id": attendant_id,
            "line_items": [{"product_id": product_id, "quantity": 3}],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "READY_FOR_PAYMENT"
    assert body["currency"] == "KES"
    assert body["total_minor"] == 8000 * 3
    assert body["line_items"][0]["line_total_minor"] == 8000 * 3


def test_sale_ignores_client_supplied_price_and_uses_catalog_price(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    # The line-item schema has no price field at all — a client cannot set
    # its own price even if it tries to smuggle one in.
    resp = client.post(
        f"/tenants/{tenant_id}/sales",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "till_id": till_id,
            "attendant_id": attendant_id,
            "line_items": [{"product_id": product_id, "quantity": 1, "unit_price_minor": 1}],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["total_minor"] == 8000


def test_create_sale_requires_at_least_one_line_item(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str
) -> None:
    resp = client.post(
        f"/tenants/{tenant_id}/sales",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"till_id": till_id, "attendant_id": attendant_id, "line_items": []},
    )
    assert resp.status_code == 422


def test_create_sale_rejects_zero_quantity(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    resp = client.post(
        f"/tenants/{tenant_id}/sales",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "till_id": till_id,
            "attendant_id": attendant_id,
            "line_items": [{"product_id": product_id, "quantity": 0}],
        },
    )
    assert resp.status_code == 422


def test_create_sale_requires_idempotency_key_header(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    resp = client.post(
        f"/tenants/{tenant_id}/sales",
        json={
            "till_id": till_id,
            "attendant_id": attendant_id,
            "line_items": [{"product_id": product_id, "quantity": 1}],
        },
    )
    assert resp.status_code == 422


def test_sale_rejects_product_from_another_tenant(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str
) -> None:
    other_tenant = client.post("/tenants", json={"name": "Other Duka"}).json()["id"]
    other_product = client.post(
        f"/tenants/{other_tenant}/products",
        json={"sku": "SODA-500", "name": "Soda 500ml", "price_minor": 8000, "currency": "KES"},
    ).json()

    resp = client.post(
        f"/tenants/{tenant_id}/sales",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "till_id": till_id,
            "attendant_id": attendant_id,
            "line_items": [{"product_id": other_product["id"], "quantity": 1}],
        },
    )
    assert resp.status_code == 400
