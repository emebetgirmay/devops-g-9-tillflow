from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import app
from app.models import Base
from app.payments_client import get_payments_client


class FakePaymentsClient:
    """Deterministic stand-in for the real Payments API in POS-side tests.

    Records every call it receives so tests can assert on what POS sent
    (sale_id, amount, idempotency_key, ...) without a network dependency.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request_payment(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"payment_id": f"pay-{uuid.uuid4()}", "status": "PENDING"}


@pytest.fixture()
def fake_payments_client() -> FakePaymentsClient:
    return FakePaymentsClient()


@pytest.fixture()
def client(fake_payments_client: FakePaymentsClient) -> TestClient:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_payments_client] = lambda: fake_payments_client

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture()
def tenant_id(client: TestClient) -> str:
    resp = client.post("/tenants", json={"name": "Acme Duka"})
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.fixture()
def till_id(client: TestClient, tenant_id: str) -> str:
    resp = client.post(f"/tenants/{tenant_id}/tills", json={"name": "Till 1"})
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.fixture()
def attendant_id(client: TestClient, tenant_id: str) -> str:
    resp = client.post(
        f"/tenants/{tenant_id}/attendants",
        json={"name": "Mary", "phone": "254712345678", "role": "ATTENDANT"},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.fixture()
def product_id(client: TestClient, tenant_id: str) -> str:
    resp = client.post(
        f"/tenants/{tenant_id}/products",
        json={"sku": "SODA-500", "name": "Soda 500ml", "price_minor": 8000, "currency": "KES"},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def create_ready_sale(client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str, quantity: int = 2, idempotency_key: str | None = None) -> dict:
    resp = client.post(
        f"/tenants/{tenant_id}/sales",
        headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())},
        json={
            "till_id": till_id,
            "attendant_id": attendant_id,
            "line_items": [{"product_id": product_id, "quantity": quantity}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
