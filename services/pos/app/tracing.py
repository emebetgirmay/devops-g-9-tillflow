"""Request trace id for POS, the same W3C ``traceparent`` contract Payments and Commission use
(ADR 0009 section 5): take the caller's trace id or start one, log it on the request line, return
it as ``X-Trace-Id``, and pass it to Payments, so one id follows a sale into its payment.

A ContextVar, not a global: requests run concurrently.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass

from . import spans

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_current: ContextVar[str] = ContextVar("trace_id", default="")


@dataclass(frozen=True)
class RequestSpan:
    """The request's own span (app/spans.py): what the call to Payments hangs under in X-Ray."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_ns: int


_request_span: ContextVar[RequestSpan | None] = ContextVar("request_span", default=None)


def start(traceparent: str | None) -> str:
    """Adopt the trace id of a well-formed ``traceparent``, else start a new one (time-based, so
    X-Ray accepts it); remember the caller's span as this request's parent."""
    parts = (traceparent or "").strip().lower().split("-")
    trace_id = parts[1] if len(parts) == 4 else ""
    parent = None
    if not _TRACE_ID_RE.match(trace_id) or trace_id == "0" * 32:
        trace_id = spans.new_trace_id()
    elif _SPAN_ID_RE.match(parts[2]) and parts[2] != "0" * 16:
        parent = parts[2]
    _current.set(trace_id)
    _request_span.set(RequestSpan(trace_id, spans.new_span_id(), parent, spans.now_ns()))
    return trace_id


def current_request_span() -> RequestSpan | None:
    return _request_span.get()


def traceparent(span_id: str | None = None) -> str | None:
    """Header for an outbound call, or None outside a request (the background reconcile loop).
    ``span_id`` is the outbound call's own span, so Payments' span nests under it."""
    trace_id = _current.get()
    return f"00-{trace_id}-{span_id or spans.new_span_id()}-01" if trace_id else None
