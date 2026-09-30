"""Spans to AWS X-Ray through the task's ADOT sidecar (ADR 0009 section 5, ADR 0010).

Same module as services/payments/core/spans.py (each service is its own image); keep them in step.

The trace id already follows a sale into its payment and callback (app/tracing.py); this module
records the timed spans that turn it into an X-Ray waterfall. Standard library only: spans are sent
as OTLP/HTTP JSON to the sidecar's receiver (``$OTEL_EXPORTER_OTLP_ENDPOINT/v1/traces``), whose
``awsxray`` exporter forwards them (infra/envs/sandbox/ecs.tf, the ADOT config).

Never in the request's way: ``record()`` only puts the span on a bounded queue; a background thread
sends batches and drops what it cannot send (counted, never raised). With no endpoint set (tests,
local runs) nothing is started or sent, and it only ever sends to the sidecar on loopback: any
other endpoint is refused. X-Ray only accepts trace ids whose first 8 hex digits are
the current Unix time, which is what ``new_trace_id()`` makes.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

SERVER, CLIENT, INTERNAL = 2, 3, 1  # OTLP span kinds

_BATCH = 256
_FLUSH_SECONDS = 2.0
_QUEUE_MAX = 4096


def new_trace_id() -> str:
    """W3C trace id that X-Ray accepts: 8 hex digits of Unix time, then 24 random."""
    return f"{int(time.time()):08x}{secrets.token_hex(12)}"


def new_span_id() -> str:
    return secrets.token_hex(8)


def now_ns() -> int:
    return time.time_ns()


class _Exporter:
    def __init__(self, endpoint: str, service: str, send: Callable[[bytes], None] | None = None) -> None:
        self.url = endpoint.rstrip("/") + "/v1/traces"
        self.service = service
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_MAX)
        self.dropped = 0
        self._send = send or self._post
        threading.Thread(target=self._run, name="span-exporter", daemon=True).start()

    def put(self, span: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(span)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while True:
            batch = [self.queue.get()]
            deadline = time.monotonic() + _FLUSH_SECONDS
            while len(batch) < _BATCH:
                try:
                    batch.append(self.queue.get(timeout=max(0.0, deadline - time.monotonic())))
                except queue.Empty:
                    break
            try:
                self._send(self._payload(batch))
            except Exception:  # noqa: BLE001 - tracing must never take the service down
                self.dropped += len(batch)

    def _payload(self, spans: list[dict[str, Any]]) -> bytes:
        return json.dumps({
            "resourceSpans": [{
                "resource": {"attributes": _attributes({"service.name": self.service})},
                "scopeSpans": [{"scope": {"name": "tillflow"}, "spans": spans}],
            }]
        }).encode()

    def _post(self, body: bytes) -> None:
        request = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - fixed local URL
            response.read()


_exporter: _Exporter | None = None
_lock = threading.Lock()
_sink: list[dict[str, Any]] | None = None  # tests: capture spans instead of sending them


def capture(sink: list[dict[str, Any]] | None) -> None:
    """Tests: collect recorded spans in ``sink`` (None to stop)."""
    global _sink
    _sink = sink


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def sidecar_endpoint() -> str | None:
    """The OTLP endpoint if it is the task's own sidecar on loopback, else None (nothing sent)."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK:
        return None
    return endpoint


def _get_exporter() -> _Exporter | None:
    global _exporter
    endpoint = sidecar_endpoint()
    if endpoint is None:
        return None
    with _lock:
        if _exporter is None:
            _exporter = _Exporter(endpoint, os.environ.get("OTEL_SERVICE_NAME", "service"))
    return _exporter


def _attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out.append({"key": key, "value": {"boolValue": value}})
        elif isinstance(value, int):
            out.append({"key": key, "value": {"intValue": str(value)}})
        else:
            out.append({"key": key, "value": {"stringValue": str(value)}})
    return out


def record(
    *,
    trace_id: str,
    span_id: str,
    name: str,
    kind: int,
    start_ns: int,
    end_ns: int,
    parent_span_id: str | None = None,
    attributes: dict[str, Any] | None = None,
    error: bool = False,
) -> None:
    """Queue one finished span. Never raises."""
    if not trace_id:
        return
    span = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": kind,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(max(end_ns, start_ns)),
        "attributes": _attributes(attributes or {}),
        "status": {"code": 2 if error else 1},
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    if _sink is not None:
        _sink.append(span)
        return
    exporter = _get_exporter()
    if exporter is not None:
        exporter.put(span)
