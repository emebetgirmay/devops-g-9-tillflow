"""Span export for the X-Ray waterfall: loopback only, never in a request's way, and the callback
joins the sale's trace as a child of the request that created the payment."""

from __future__ import annotations

import os
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
from helpers import ServiceTestCase, key, payment_body

from core import otlp

SIDECAR = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"}
SALE_TRACE = "0af7651916cd43dd8448eb211c80319c"
POS_SPAN = "b7ad6b7169203331"
SALE_TRACEPARENT = f"00-{SALE_TRACE}-{POS_SPAN}-01"


class EndpointTest(unittest.TestCase):
    def test_only_a_loopback_collector_is_used(self) -> None:
        for url, expected in (
            ("http://127.0.0.1:4318", "http://127.0.0.1:4318"),
            ("http://localhost:4318/", "http://localhost:4318"),
            ("https://collector.example.com:4318", None),
            ("http://10.0.1.5:4318", None),
            ("", None),
        ):
            with self.subTest(url=url), mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": url}):
                self.assertEqual(otlp.endpoint(), expected)

    def test_export_is_a_no_op_without_a_sidecar(self) -> None:
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": ""}), \
                mock.patch.object(otlp._queue, "put_nowait") as put:
            otlp.export(trace_id=SALE_TRACE, span_id=POS_SPAN, name="x", start=1.0, end=2.0)
        put.assert_not_called()

    def test_a_full_queue_drops_the_span_and_does_not_raise(self) -> None:
        with mock.patch.dict(os.environ, SIDECAR), mock.patch.object(otlp._worker_started, "is_set", return_value=True), \
                mock.patch.object(otlp._queue, "put_nowait", side_effect=otlp.queue.Full):
            otlp.export(trace_id=SALE_TRACE, span_id=POS_SPAN, name="x", start=1.0, end=2.0)

    def test_payload_is_otlp_json(self) -> None:
        with mock.patch.dict(os.environ, {"OTEL_SERVICE_NAME": "payments"}):
            body = otlp.payload({
                "trace_id": SALE_TRACE, "span_id": POS_SPAN, "parent_span_id": "00f067aa0ba902b7",
                "name": "POST /payments", "start": 1.5, "end": 2.0,
                "attributes": {"http.status_code": 201}, "error": False,
            })
        resource = body["resourceSpans"][0]
        self.assertEqual(resource["resource"]["attributes"][0]["value"], {"stringValue": "payments"})
        (span,) = resource["scopeSpans"][0]["spans"]
        self.assertEqual((span["traceId"], span["spanId"], span["parentSpanId"]), (SALE_TRACE, POS_SPAN, "00f067aa0ba902b7"))
        self.assertEqual((span["startTimeUnixNano"], span["endTimeUnixNano"]), ("1500000000", "2000000000"))
        self.assertEqual(span["status"], {"code": 1})
        self.assertEqual(span["attributes"], [{"key": "http.status_code", "value": {"intValue": "201"}}])

    def test_only_ints_are_sent_as_int_values(self) -> None:
        body = otlp.payload({
            "trace_id": SALE_TRACE, "span_id": POS_SPAN, "name": "x", "start": 1.0, "end": 2.0,
            "attributes": {"http.method": "POST", "http.status_code": 500, "flag": True},
        })
        values = {a["key"]: a["value"] for a in body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        self.assertEqual(values["http.method"], {"stringValue": "POST"})
        self.assertEqual(values["http.status_code"], {"intValue": "500"})
        self.assertEqual(values["flag"], {"stringValue": "True"})


class WaterfallTest(ServiceTestCase):
    """What X-Ray receives for one sale: Payments' create span under POS's span, then the
    callback as a child of that create span, in the same trace."""

    def spans(self, exported: mock.Mock) -> list[dict]:
        return [call.kwargs for call in exported.call_args_list]

    def test_create_then_callback_form_one_tree(self) -> None:
        with mock.patch.object(otlp, "export") as exported:
            headers = {"Idempotency-Key": key(1), "traceparent": SALE_TRACEPARENT}
            created = self.call("POST", "/payments", payment_body(msisdn="254000000001"), headers)
            self.advance(2)
            self.call("POST", "/_fake/deliver-callbacks", {})
        spans = self.spans(exported)
        (create,) = [s for s in spans if s["name"] == "POST /payments"]
        self.assertEqual((create["trace_id"], create["parent_span_id"]), (SALE_TRACE, POS_SPAN))
        self.assertEqual(create["attributes"]["http.status_code"], 201)

        (callback,) = [s for s in spans if s["name"] == "payment PENDING to SUCCEEDED"]
        self.assertEqual(callback["trace_id"], SALE_TRACE)
        self.assertEqual(callback["parent_span_id"], create["span_id"])
        self.assertEqual(callback["attributes"]["tillflow.record_id"], created.body["payment_id"])
        self.assertNotEqual(callback["attributes"]["tillflow.request_trace_id"], SALE_TRACE)

    def test_probes_export_nothing_and_a_5xx_is_marked_an_error(self) -> None:
        with mock.patch.object(otlp, "export") as exported:
            self.call("GET", "/health")
            with mock.patch.object(self.app, "_get", side_effect=RuntimeError("boom")):
                self.call("GET", "/payments/pay_x")
        (span,) = self.spans(exported)
        self.assertTrue(span["error"])

    def test_a_trace_we_start_has_an_xray_shaped_id_and_no_parent(self) -> None:
        with mock.patch.object(otlp, "export") as exported:
            self.pay(key(2), msisdn="254000000001")
        (span,) = self.spans(exported)
        self.assertEqual(span["parent_span_id"], "")
        self.assertAlmostEqual(int(span["trace_id"][:8], 16), __import__("time").time(), delta=5)


if __name__ == "__main__":
    unittest.main()
