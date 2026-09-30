"""X-Ray spans (app/spans.py): each POS request is a span under its caller's, and each call to
Payments is a client span under the request's whose id Payments receives as its parent."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import payments_client, spans, tracing

TRACE = spans.new_trace_id()
CALLER_SPAN = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE}-{CALLER_SPAN}-01"


@pytest.fixture
def recorded():
    sink: list[dict] = []
    spans.capture(sink)
    yield sink
    spans.capture(None)


def test_a_request_is_a_server_span_under_the_callers_span(client: TestClient, recorded) -> None:
    client.post("/tenants", json={"name": "Acme Duka"}, headers={"traceparent": TRACEPARENT})
    (span,) = [s for s in recorded if s["name"] == "POST /tenants"]
    assert (span["traceId"], span["parentSpanId"], span["kind"]) == (TRACE, CALLER_SPAN, spans.SERVER)
    assert {"key": "http.status_code", "value": {"intValue": "201"}} in span["attributes"]


def test_probes_make_no_spans(client: TestClient, recorded) -> None:
    client.get("/health")
    client.get("/metrics")
    assert recorded == []


def test_new_trace_ids_are_time_based_for_x_ray(client: TestClient) -> None:
    trace_id = client.post("/tenants", json={"name": "A"}).headers["X-Trace-Id"]
    assert abs(int(trace_id[:8], 16) - time.time()) < 5


def test_the_call_to_payments_is_a_client_span_payments_nests_under(monkeypatch, recorded) -> None:
    sent: list[dict] = []

    class Recorder:
        def __init__(self, *args, **kwargs) -> None: ...

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None: ...

        def post(self, url, json=None, headers=None):
            sent.append(dict(headers or {}))
            return type("R", (), {"status_code": 201, "raise_for_status": lambda s: None, "json": lambda s: {}})()

    monkeypatch.setattr(payments_client.httpx, "Client", Recorder)
    tracing.start(TRACEPARENT)
    request = tracing.current_request_span()
    payments_client.PaymentsClient("http://payments.test").request_payment(
        sale_id="s1", tenant_id="t1", amount_minor=1000, currency="KES",
        msisdn="254000000001", idempotency_key="k" * 16,
    )
    (span,) = recorded
    assert (span["name"], span["kind"], span["parentSpanId"]) == ("payments POST /payments", spans.CLIENT, request.span_id)
    # Payments' request span will have this client span as its parent.
    assert sent[0]["traceparent"] == f"00-{TRACE}-{span['spanId']}-01"
