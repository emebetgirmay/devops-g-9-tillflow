from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.models import Sale
from tests.conftest import create_ready_sale


def _mark_paid(client: TestClient, tenant_id: str, sale_id: str) -> None:
    # A sale can only reach PAID via PAYMENT_REQUESTED (state.py's transition table), so request
    # a payment first (the fake Payments client hands back a payment_id) and only then push the
    # PAID event for it — matching how the real integration reaches PAID.
    resp = client.post(
        f"/tenants/{tenant_id}/sales/{sale_id}/payment-request", json={"phone": "254712345678"}
    )
    assert resp.status_code == 200, resp.text
    payment_id = resp.json()["payment_id"]

    resp = client.post(
        f"/internal/sales/{sale_id}/payment-events",
        json={
            "event_id": f"evt-{uuid.uuid4()}",
            "payment_id": payment_id,
            "status": "PAID",
            "amount_minor": 16000,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "PAID"


def _paid_sale(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> str:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    _mark_paid(client, tenant_id, sale["id"])
    return sale["id"]


def _window() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=1)).isoformat()
    until = (now + timedelta(hours=1)).isoformat()
    return since, until


def test_lists_only_paid_sales_with_the_attendants_rate(
    client: TestClient, tenant_id: str, till_id: str, product_id: str
) -> None:
    resp = client.post(
        f"/tenants/{tenant_id}/attendants",
        json={"name": "Mary", "phone": "254712345678", "commission_rate_bps": 500},
    )
    attendant_id = resp.json()["id"]

    paid_id = _paid_sale(client, tenant_id, till_id, attendant_id, product_id)
    create_ready_sale(
        client, tenant_id, till_id, attendant_id, product_id
    )  # left READY_FOR_PAYMENT

    since, until = _window()
    resp = client.get(
        f"/tenants/{tenant_id}/commission/paid-sales", params={"since": since, "until": until}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [s["sale_id"] for s in body["sales"]] == [paid_id]
    assert body["sales"][0]["commission_rate_bps"] == 500
    assert body["sales"][0]["total_minor"] == 16000
    assert body["sales"][0]["currency"] == "KES"
    assert body["sales"][0]["attendant_msisdn"] == "254712345678"
    assert body["next_cursor"] is None


def test_excludes_failed_and_void_sales(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    sale = create_ready_sale(client, tenant_id, till_id, attendant_id, product_id)
    resp = client.post(
        f"/tenants/{tenant_id}/sales/{sale['id']}/payment-request", json={"phone": "254712345678"}
    )
    payment_id = resp.json()["payment_id"]
    resp = client.post(
        f"/internal/sales/{sale['id']}/payment-events",
        json={
            "event_id": f"evt-{uuid.uuid4()}",
            "payment_id": payment_id,
            "status": "PAYMENT_FAILED",
            "amount_minor": 16000,
        },
    )
    assert resp.status_code == 200
    since, until = _window()
    resp = client.get(
        f"/tenants/{tenant_id}/commission/paid-sales", params={"since": since, "until": until}
    )
    assert resp.json()["sales"] == []


def test_filters_by_window_and_attendant(
    client: TestClient, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    inside = _paid_sale(client, tenant_id, till_id, attendant_id, product_id)
    since, until = _window()

    # Outside the window: nothing comes back.
    far_future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    far_future_end = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    resp = client.get(
        f"/tenants/{tenant_id}/commission/paid-sales",
        params={"since": far_future, "until": far_future_end},
    )
    assert resp.json()["sales"] == []

    # Wrong attendant: nothing comes back.
    resp = client.post(
        f"/tenants/{tenant_id}/attendants", json={"name": "Other", "phone": "254700000001"}
    )
    other_attendant = resp.json()["id"]
    resp = client.get(
        f"/tenants/{tenant_id}/commission/paid-sales",
        params={"since": since, "until": until, "attendant_id": other_attendant},
    )
    assert resp.json()["sales"] == []

    resp = client.get(
        f"/tenants/{tenant_id}/commission/paid-sales",
        params={"since": since, "until": until, "attendant_id": attendant_id},
    )
    assert [s["sale_id"] for s in resp.json()["sales"]] == [inside]


def test_pagination_cursor_covers_every_sale_exactly_once_including_a_timestamp_tie(
    client, tenant_id: str, till_id: str, attendant_id: str, product_id: str
) -> None:
    ids = [_paid_sale(client, tenant_id, till_id, attendant_id, product_id) for _ in range(5)]

    # Force two of them to share an updated_at, the case the keyset cursor exists to handle.
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        rows = db.query(Sale).filter(Sale.id.in_(ids[:2])).all()
        tied = datetime.now(timezone.utc)
        for row in rows:
            row.updated_at = tied
        db.commit()
    finally:
        db.close()

    since, until = _window()
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"since": since, "until": until, "limit": 2}
        if cursor:
            params["cursor"] = cursor
        resp = client.get(f"/tenants/{tenant_id}/commission/paid-sales", params=params)
        assert resp.status_code == 200
        body = resp.json()
        seen += [s["sale_id"] for s in body["sales"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    else:
        raise AssertionError("pagination did not terminate")

    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(set(seen))  # no sale handed out twice


def test_rejects_a_bad_window_or_cursor(client: TestClient, tenant_id: str) -> None:
    since, until = _window()
    resp = client.get(
        f"/tenants/{tenant_id}/commission/paid-sales", params={"since": until, "until": since}
    )
    assert resp.status_code == 400

    resp = client.get(
        f"/tenants/{tenant_id}/commission/paid-sales",
        params={"since": since, "until": until, "cursor": "not-base64!!"},
    )
    assert resp.status_code == 400


def test_unknown_tenant_is_404(client: TestClient) -> None:
    since, until = _window()
    resp = client.get(
        "/tenants/does-not-exist/commission/paid-sales", params={"since": since, "until": until}
    )
    assert resp.status_code == 404
