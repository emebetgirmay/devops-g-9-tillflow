"""app/scheduler.py: the background sweep that reconciles sales stuck in
PAYMENT_REQUESTED, since app/routers/sales.py::reconcile_payment is purely
on-demand — nothing calls it unless something asks.
"""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from app import scheduler
from tests.conftest import FakePaymentsClient, create_ready_sale


def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("POS_RECONCILE_SCHEDULER", raising=False)
    assert scheduler.is_enabled() is False


def test_enabled_reads_truthy_values(monkeypatch) -> None:
    for value in ("1", "true", "True", "yes", "on"):
        monkeypatch.setenv("POS_RECONCILE_SCHEDULER", value)
        assert scheduler.is_enabled() is True
    monkeypatch.setenv("POS_RECONCILE_SCHEDULER", "0")
    assert scheduler.is_enabled() is False


def test_run_once_reconciles_stale_payment_requested_sales(
    client,
    session_factory: sessionmaker,
    tenant_id: str,
    till_id: str,
    attendant_id: str,
    product_id: str,
    fake_payments_client: FakePaymentsClient,
    monkeypatch,
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    req = client.post(
        f"/tenants/{tenant_id}/sales/{sale['id']}/payment-request", json={"phone": "254712345678"}
    )
    payment_id = req.json()["payment_id"]
    fake_payments_client.set_state(payment_id, "SUCCEEDED", amount_minor=sale["total_minor"])

    monkeypatch.setenv("POS_RECONCILE_STALE_AFTER_SECONDS", "0")
    attempted = scheduler.run_once(session_factory=session_factory, get_client=lambda: fake_payments_client)
    assert attempted == 1

    sale_after = client.get(f"/tenants/{tenant_id}/sales/{sale['id']}").json()
    assert sale_after["status"] == "PAID"


def test_run_once_skips_fresh_payment_requested_sales(
    client,
    session_factory: sessionmaker,
    tenant_id: str,
    till_id: str,
    attendant_id: str,
    product_id: str,
    fake_payments_client: FakePaymentsClient,
    monkeypatch,
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    req = client.post(
        f"/tenants/{tenant_id}/sales/{sale['id']}/payment-request", json={"phone": "254712345678"}
    )
    fake_payments_client.set_state(req.json()["payment_id"], "SUCCEEDED", amount_minor=sale["total_minor"])

    # A sale that just requested payment moments ago isn't "stale" yet.
    monkeypatch.setenv("POS_RECONCILE_STALE_AFTER_SECONDS", "3600")
    attempted = scheduler.run_once(session_factory=session_factory, get_client=lambda: fake_payments_client)
    assert attempted == 0

    sale_after = client.get(f"/tenants/{tenant_id}/sales/{sale['id']}").json()
    assert sale_after["status"] == "PAYMENT_REQUESTED"


def test_run_once_ignores_sales_not_awaiting_payment(
    client,
    session_factory: sessionmaker,
    tenant_id: str,
    till_id: str,
    attendant_id: str,
    product_id: str,
    fake_payments_client: FakePaymentsClient,
    monkeypatch,
) -> None:
    # A READY_FOR_PAYMENT sale (no payment requested yet) must never be
    # picked up by the sweep — it has no payment_id to reconcile against.
    create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)

    monkeypatch.setenv("POS_RECONCILE_STALE_AFTER_SECONDS", "0")
    attempted = scheduler.run_once(session_factory=session_factory, get_client=lambda: fake_payments_client)
    assert attempted == 0


def test_one_bad_sale_does_not_stop_the_sweep(
    client,
    session_factory: sessionmaker,
    tenant_id: str,
    till_id: str,
    attendant_id: str,
    product_id: str,
    fake_payments_client: FakePaymentsClient,
    monkeypatch,
) -> None:
    good = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    bad = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)

    good_req = client.post(
        f"/tenants/{tenant_id}/sales/{good['id']}/payment-request", json={"phone": "254712345678"}
    ).json()
    bad_req = client.post(
        f"/tenants/{tenant_id}/sales/{bad['id']}/payment-request", json={"phone": "254712345678"}
    ).json()

    fake_payments_client.set_state(good_req["payment_id"], "SUCCEEDED", amount_minor=good["total_minor"])
    # Wrong amount — will raise ReconcileAmountMismatch inside the sweep.
    fake_payments_client.set_state(bad_req["payment_id"], "SUCCEEDED", amount_minor=bad["total_minor"] + 1)

    monkeypatch.setenv("POS_RECONCILE_STALE_AFTER_SECONDS", "0")
    attempted = scheduler.run_once(session_factory=session_factory, get_client=lambda: fake_payments_client)
    assert attempted == 2

    good_after = client.get(f"/tenants/{tenant_id}/sales/{good['id']}").json()
    bad_after = client.get(f"/tenants/{tenant_id}/sales/{bad['id']}").json()
    assert good_after["status"] == "PAID"
    assert bad_after["status"] == "PAYMENT_REQUESTED"  # untouched, not crashed
