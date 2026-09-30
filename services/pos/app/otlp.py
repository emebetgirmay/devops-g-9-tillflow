"""Span export to the ADOT sidecar as OTLP/HTTP JSON, so X-Ray shows a waterfall (ADR 0009).

Same exporter as services/payments/core/otlp.py (the two images share no code), standard library
only, no OpenTelemetry SDK:

- Loopback only. The endpoint must be the sidecar on 127.0.0.1/localhost; anything else disables
  export.
- Never in the request's way. Spans go on a bounded queue drained by one daemon thread; a full
  queue drops the span, and a slow or dead collector costs a request nothing.

Off (a no-op) unless OTEL_EXPORTER_OTLP_ENDPOINT is set, which only the deployed task does.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import urllib.request
from urllib.parse import urlparse

INTERNAL, SERVER = 1, 2  # OTLP SpanKind
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_queue: queue.Queue[dict] = queue.Queue(maxsize=1000)
_worker_started = threading.Event()


def endpoint() -> str | None:
    url = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip().rstrip("/")
    return url if url and urlparse(url).hostname in _LOOPBACK else None


def payload(span: dict) -> dict:
    """One span as an OTLP/HTTP JSON ExportTraceServiceRequest. Ids are hex strings."""
    # Ints go as intValue (a string in OTLP JSON): the X-Ray translator reads http.status_code
    # only as an int, and records status 0 for a stringValue.
    attributes = [
        {
            "key": key,
            "value": {"intValue": str(value)}
            if isinstance(value, int) and not isinstance(value, bool)
            else {"stringValue": str(value)},
        }
        for key, value in span.get("attributes", {}).items()
    ]
    body = {
        "traceId": span["trace_id"],
        "spanId": span["span_id"],
        "name": span["name"],
        "kind": span.get("kind", SERVER),
        "startTimeUnixNano": str(int(span["start"] * 1e9)),
        "endTimeUnixNano": str(int(span["end"] * 1e9)),
        "attributes": attributes,
        "status": {"code": 2 if span.get("error") else 1},
    }
    if span.get("parent_span_id"):
        body["parentSpanId"] = span["parent_span_id"]
    service = os.environ.get("OTEL_SERVICE_NAME", "pos")
    resource = {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]}
    return {"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": [body]}]}]}


def export(**span) -> None:
    """Queue a span: trace_id, span_id, name, start, end (epoch seconds), and optionally
    parent_span_id, kind, attributes, error. Returns at once; never raises."""
    if endpoint() is None or not span.get("trace_id"):
        return
    if not _worker_started.is_set():
        _worker_started.set()
        threading.Thread(target=_drain, name="otlp-export", daemon=True).start()
    try:
        _queue.put_nowait(span)
    except queue.Full:
        pass  # tracing is best effort; the request is not


def _drain() -> None:
    while True:
        span = _queue.get()
        url = endpoint()
        if url is None:
            continue
        request = urllib.request.Request(
            url + "/v1/traces",
            data=json.dumps(payload(span)).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=2).close()
        except Exception:  # noqa: BLE001 - a lost span must never surface
            pass
