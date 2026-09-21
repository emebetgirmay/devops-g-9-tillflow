from __future__ import annotations

from fastapi.testclient import TestClient


def test_create_tenant(client: TestClient) -> None:
    resp = client.post("/tenants", json={"name": "Acme Duka"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "Acme Duka"


def test_create_attendant_rejects_unknown_role(client: TestClient, tenant_id: str) -> None:
    resp = client.post(
        f"/tenants/{tenant_id}/attendants",
        json={"name": "Mary", "phone": "254712345678", "role": "MANAGER"},
    )
    assert resp.status_code == 422


def test_create_attendant_rejects_bad_phone(client: TestClient, tenant_id: str) -> None:
    resp = client.post(
        f"/tenants/{tenant_id}/attendants",
        json={"name": "Mary", "phone": "0712345678", "role": "ATTENDANT"},
    )
    assert resp.status_code == 422


def test_attendant_scoped_to_unknown_tenant_is_404(client: TestClient) -> None:
    resp = client.post(
        "/tenants/does-not-exist/attendants",
        json={"name": "Mary", "phone": "254712345678", "role": "ATTENDANT"},
    )
    assert resp.status_code == 404


def test_product_sku_unique_per_tenant(client: TestClient, tenant_id: str) -> None:
    body = {"sku": "SODA-500", "name": "Soda 500ml", "price_minor": 8000, "currency": "KES"}
    first = client.post(f"/tenants/{tenant_id}/products", json=body)
    assert first.status_code == 201

    duplicate = client.post(f"/tenants/{tenant_id}/products", json=body)
    assert duplicate.status_code == 409


def test_product_rejects_non_positive_price(client: TestClient, tenant_id: str) -> None:
    resp = client.post(
        f"/tenants/{tenant_id}/products",
        json={"sku": "FREEBIE", "name": "Freebie", "price_minor": 0, "currency": "KES"},
    )
    assert resp.status_code == 422
