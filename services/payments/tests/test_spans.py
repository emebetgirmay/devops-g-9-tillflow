"""X-Ray spans (core/spans.py): each request is a span under its caller's, each state change a
span under the request's, and a callback that settles a payment also shows in the sale's trace."""

from __future__ import annotations

import io
import json
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import _bootstrap  # noqa: F401
from helpers import ServiceTestCase, key, payment_body

from core import spans

SALE_TRACE = spans.new_trace_id()
POS_SPAN = "b7ad6b7169203331"
SALE_TRACEPARENT = f"00-{SALE_TRACE}-{POS_SPAN}-01"


class SpanCase(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.spans: list[dict] = []
        spans.capture(self.spans)
        self.addCleanup(spans.capture, None)

    def quiet_call(self, method: str, path: str, body: dict, headers: dict) -> dict:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return self.call(method, path, body, headers).body

    def named(self, name: str) -> list[dict]:
        return [s for s in self.spans if s["name"] == name]


class RequestSpansTest(SpanCase):
    def test_payment_create_is_a_server_span_under_the_callers_span(self) -> None:
        self.quiet_call("POST", "/payments", payment_body(msisdn="254000000001"),
                        {"Idempotency-Key": key(1), "traceparent": SALE_TRACEPARENT})
        (request,) = self.named("POST /payments")
        self.assertEqual((request["traceId"], request["parentSpanId"]), (SALE_TRACE, POS_SPAN))
        self.assertEqual(request["kind"], spans.SERVER)
        self.assertIn({"key": "http.status_code", "value": {"intValue": "201"}}, request["attributes"])
        # The state change hangs under the request.
        (created,) = [s for s in self.spans if s["name"].startswith("payment ") and s["traceId"] == SALE_TRACE]
        self.assertEqual(created["parentSpanId"], request["spanId"])

    def test_callback_that_settles_the_payment_shows_in_the_sale_trace(self) -> None:
        self.quiet_call("POST", "/payments", payment_body(msisdn="254000000001"),
                        {"Idempotency-Key": key(2), "traceparent": SALE_TRACEPARENT})
        self.advance(2)
        self.quiet_call("POST", "/_fake/deliver-callbacks", {}, {})  # the provider sends no traceparent
        (settled,) = [s for s in self.spans if s["traceId"] == SALE_TRACE and s["name"].endswith(" to SUCCEEDED")]
        self.assertEqual(settled["name"], "payments: payment PENDING to SUCCEEDED")
        via = {a["key"]: a["value"]["stringValue"] for a in settled["attributes"]}["tillflow.via_trace_id"]
        self.assertNotEqual(via, SALE_TRACE)  # the callback request's own trace
        self.assertTrue(self.named("POST /_fake/deliver-callbacks"))

    def test_probes_and_scrapes_make_no_spans(self) -> None:
        self.quiet_call("GET", "/health", {}, {})
        self.quiet_call("GET", "/metrics", {}, {})
        self.assertEqual(self.spans, [])


class TraceIdsTest(unittest.TestCase):
    def test_new_trace_ids_start_with_the_unix_time_as_x_ray_requires(self) -> None:
        trace_id = spans.new_trace_id()
        self.assertRegex(trace_id, r"^[0-9a-f]{32}$")
        self.assertLess(abs(int(trace_id[:8], 16) - time.time()), 5)


class ExporterTest(unittest.TestCase):
    def test_only_the_sidecar_on_loopback_is_ever_an_endpoint(self) -> None:
        for endpoint, allowed in (
            ("http://127.0.0.1:4318", True),
            ("http://localhost:4318", True),
            ("", False),
            ("http://10.0.0.5:4318", False),
            ("https://collector.example.com", False),
        ):
            with self.subTest(endpoint=endpoint), mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint}):
                self.assertEqual(spans.sidecar_endpoint() is not None, allowed)

    def test_batches_are_otlp_json_with_the_service_name(self) -> None:
        sent: list[bytes] = []
        exporter = spans._Exporter("http://127.0.0.1:4318", "payments", send=sent.append)
        exporter.put({"traceId": SALE_TRACE, "spanId": "00f067aa0ba902b7", "name": "x"})
        deadline = time.monotonic() + 5
        while not sent and time.monotonic() < deadline:
            time.sleep(0.05)
        body = json.loads(sent[0])
        resource = body["resourceSpans"][0]
        self.assertIn({"key": "service.name", "value": {"stringValue": "payments"}},
                      resource["resource"]["attributes"])
        self.assertEqual(resource["scopeSpans"][0]["spans"][0]["name"], "x")
        self.assertEqual(exporter.url, "http://127.0.0.1:4318/v1/traces")

    def test_a_failing_sidecar_drops_spans_and_never_raises(self) -> None:
        def broken(_: bytes) -> None:
            raise OSError("connection refused")

        exporter = spans._Exporter("http://127.0.0.1:4318", "payments", send=broken)
        exporter.put({"name": "x"})
        deadline = time.monotonic() + 5
        while exporter.dropped == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(exporter.dropped, 1)


if __name__ == "__main__":
    unittest.main()
