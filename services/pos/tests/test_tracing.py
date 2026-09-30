"""A sale's trace id must reach Payments: POS adopts the caller's ``traceparent`` (or starts a
trace), returns it as ``X-Trace-Id``, logs it on the request line and sends it on to Payments."""

from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from app import otlp, payments_client, tracing

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE}-00f067aa0ba902b7-01"


def test_response_and_request_log_carry_the_callers_trace_id(client: TestClient, capsys) -> None:
    resp = client.post("/tenants", json={"name": "Acme Duka"}, headers={"traceparent": TRACEPARENT})
    assert resp.headers["X-Trace-Id"] == TRACE
    lines = [json.loads(raw) for raw in capsys.readouterr().out.splitlines() if raw.startswith("{")]
    (line,) = [x for x in lines if x.get("event") == "request"]
    assert (line["service"], line["trace_id"]) == ("pos", TRACE)
    assert line["result"] == "POST /tenants -> 201"


def test_a_request_without_traceparent_gets_a_fresh_trace_id(client: TestClient) -> None:
    first = client.post("/tenants", json={"name": "A"}).headers["X-Trace-Id"]
    second = client.post("/tenants", json={"name": "B"}).headers["X-Trace-Id"]
    assert re.fullmatch(r"[0-9a-f]{32}", first) and first != second


def test_a_malformed_traceparent_is_replaced_not_propagated(client: TestClient) -> None:
    resp = client.post("/tenants", json={"name": "A"}, headers={"traceparent": "garbage"})
    assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["X-Trace-Id"])


def test_probes_are_not_logged(client: TestClient, capsys) -> None:
    client.get("/health")
    assert '"event": "request"' not in capsys.readouterr().out


def test_payments_client_sends_the_trace_id(monkeypatch) -> None:
    sent: list[dict] = []

    class Recorder:
        def __init__(self, *args, **kwargs) -> None: ...

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None: ...

        def _reply(self, headers):
            sent.append(dict(headers or {}))
            return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {}})()

        def post(self, url, json=None, headers=None):
            return self._reply(headers)

        def get(self, url, headers=None):
            return self._reply(headers)

    monkeypatch.setattr(payments_client.httpx, "Client", Recorder)
    tracing.start(TRACEPARENT)
    pc = payments_client.PaymentsClient("http://payments.test")
    pc.request_payment(
        sale_id="s1", tenant_id="t1", amount_minor=1000, currency="KES",
        msisdn="254000000001", idempotency_key="k" * 16,
    )
    pc.get_payment("pay_1")
    assert [h["traceparent"].split("-")[1] for h in sent] == [TRACE, TRACE]
    # Payments' span must hang under this request's span, not the caller's.
    assert {h["traceparent"].split("-")[2] for h in sent} == {tracing.span_id()}
    assert tracing.parent_span_id() == "00f067aa0ba902b7"
    assert sent[0]["Idempotency-Key"] == "k" * 16


def test_each_request_exports_a_server_span_in_the_callers_trace(client: TestClient, monkeypatch) -> None:
    spans: list[dict] = []
    monkeypatch.setattr(otlp, "export", lambda **span: spans.append(span))
    client.post("/tenants", json={"name": "Acme Duka"}, headers={"traceparent": TRACEPARENT})
    client.get("/health")
    (span,) = spans
    assert (span["trace_id"], span["parent_span_id"]) == (TRACE, "00f067aa0ba902b7")
    assert (span["name"], span["attributes"]["http.status_code"]) == ("POST /tenants", 201)
    assert re.fullmatch(r"[0-9a-f]{16}", span["span_id"]) and not span["error"]
    # The status goes to X-Ray as an int, or it records status 0.
    status = [x for x in otlp.payload(span)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
              if x["key"] == "http.status_code"]
    assert status == [{"key": "http.status_code", "value": {"intValue": "201"}}]


def test_the_exporter_only_talks_to_a_loopback_collector(monkeypatch) -> None:
    for url, expected in (
        ("http://127.0.0.1:4318", "http://127.0.0.1:4318"),
        ("https://collector.example.com:4318", None),
        ("", None),
    ):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", url)
        assert otlp.endpoint() == expected
