from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import metrics
from app.db import get_db
from app.main import app
from app.models import Base
from app.payments_client import get_payments_client


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Metric objects are module-level globals (shared across the whole
    pytest process) — reset before every test so tests can assert exact
    counter values without interference from tests that ran earlier.
    """
    metrics.reset_for_tests()
    yield


class FakePaymentsClient:
    """Deterministic stand-in for the real Payments API in POS-side tests.

    Records every call it receives so tests can assert on what POS sent
    (sale_id, amount, idempotency_key, ...) without a network dependency.
    Shaped after the actual services/payments response (payment_id + state,
    not payment_id + status), since that's what reconcile_payment parses.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._payments: dict[str, dict] = {}

    def request_payment(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        # services/payments/core's real format: "pay_" + 32 hex chars, no dashes -- match it here
        # rather than uuid4()'s default 36-char-with-dashes form, which is a different length.
        payment_id = f"pay_{uuid.uuid4().hex}"
        self._payments[payment_id] = {
            "payment_id": payment_id,
            "state": "PENDING",
            "amount_minor": kwargs["amount_minor"],
        }
        return dict(self._payments[payment_id])

    def get_payment(self, payment_id: str) -> dict:
        return dict(self._payments[payment_id])

    def set_state(self, payment_id: str, state: str, amount_minor: int | None = None) -> None:
        """Test helper: simulate Payments settling (or not) a payment."""
        record = self._payments.setdefault(payment_id, {"payment_id": payment_id})
        record["state"] = state
        if amount_minor is not None:
            record["amount_minor"] = amount_minor


@pytest.fixture()
def fake_payments_client() -> FakePaymentsClient:
    return FakePaymentsClient()


@pytest.fixture()
def session_factory() -> sessionmaker:
    """The exact sessionmaker the `client` fixture's DB override uses —
    exposed separately so a test can also drive app.scheduler.run_once
    against the same in-memory database the API calls populated.

    Defaults to an isolated in-memory SQLite database, same as always. Set
    TEST_DATABASE_URL to run this exact suite against a real Postgres
    instead (CI's pos-tests job does this against a postgres:16 service
    container) — the whole point being that ADR 0002's RDS move needs no
    code change POS's own tests haven't already exercised. A real database
    is shared across the whole test run, unlike a fresh in-memory SQLite
    engine per test, so drop_all+create_all here gives every test the same
    clean-slate isolation it already had.
    """
    url = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")
    if url.startswith("sqlite"):
        engine = create_engine(
            url, connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
        )
    else:
        engine = create_engine(url, future=True)
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture()
def client(fake_payments_client: FakePaymentsClient, session_factory: sessionmaker) -> TestClient:
    def override_get_db():
        db = session_factory()
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
