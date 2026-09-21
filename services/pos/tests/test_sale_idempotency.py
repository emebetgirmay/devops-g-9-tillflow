from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import create_ready_sale


def test_repeated_create_with_same_idempotency_key_returns_same_sale(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    key = str(uuid.uuid4())
    body = {
        "till_id": till_id,
        "attendant_id": attendant_id,
        "line_items": [{"product_id": product_id, "quantity": 2}],
    }

    first = client.post(f"/tenants/{tenant_id}/sales", headers={"Idempotency-Key": key}, json=body)
    second = client.post(f"/tenants/{tenant_id}/sales", headers={"Idempotency-Key": key}, json=body)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["total_minor"] == second.json()["total_minor"]


def test_retry_with_different_payload_but_same_key_still_returns_original(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    key = str(uuid.uuid4())
    first = client.post(
        f"/tenants/{tenant_id}/sales",
        headers={"Idempotency-Key": key},
        json={
            "till_id": till_id,
            "attendant_id": attendant_id,
            "line_items": [{"product_id": product_id, "quantity": 1}],
        },
    )
    assert first.status_code == 201

    # A client that retries after a dropped response but accidentally
    # changes the quantity must not create a second sale or silently apply
    # the new quantity — idempotency wins.
    retry = client.post(
        f"/tenants/{tenant_id}/sales",
        headers={"Idempotency-Key": key},
        json={
            "till_id": till_id,
            "attendant_id": attendant_id,
            "line_items": [{"product_id": product_id, "quantity": 99}],
        },
    )
    assert retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]
    assert retry.json()["total_minor"] == first.json()["total_minor"]


def test_same_idempotency_key_is_scoped_per_tenant(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    key = str(uuid.uuid4())
    first = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id, idempotency_key=key)

    other_tenant = client.post("/tenants", json={"name": "Other Duka"}).json()["id"]
    other_till = client.post(f"/tenants/{other_tenant}/tills", json={"name": "Till 1"}).json()["id"]
    other_attendant = client.post(
        f"/tenants/{other_tenant}/attendants",
        json={"name": "Jane", "phone": "254799999999", "role": "ATTENDANT"},
    ).json()["id"]
    other_product = client.post(
        f"/tenants/{other_tenant}/products",
        json={"sku": "SODA-500", "name": "Soda 500ml", "price_minor": 5000, "currency": "KES"},
    ).json()["id"]

    second = create_ready_sale(
        client, other_tenant, other_till, other_attendant, other_product, idempotency_key=key
    )

    assert second["id"] != first["id"]
