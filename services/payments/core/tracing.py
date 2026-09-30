"""Request-scoped trace id (ADR 0009 section 5).

A W3C ``traceparent`` header in (or a fresh id generated if there wasn't
one), echoed back as ``X-Trace-Id``, and readable from anywhere while that
request is being handled — without threading a ``trace_id`` parameter
through every function between app.py's dispatch() and core/store.py's
transition(). That's what makes "one payout run followed from Commission to
Payments" possible: Commission's ledger/payments_client.py sends
``traceparent``, Payments picks the same trace id back up, and every log
line either side writes during that request carries it.

Built on ``contextvars.ContextVar``, not a plain module global, because
``ThreadingHTTPServer`` handles each request on its own thread — a global
would let two concurrent requests stomp on each other's trace id.
"""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_current: ContextVar[str] = ContextVar("trace_id", default="")
# This request's own span, and the caller's span it hangs under (empty for a trace we started).
_span: ContextVar[str] = ContextVar("span_id", default="")
_parent: ContextVar[str] = ContextVar("parent_span_id", default="")


def new_trace_id() -> str:
    """32 hex. The first 8 are the epoch second, which is the form X-Ray's own ids take, so the
    trace is accepted whatever the collector version does with plain W3C ids."""
    return f"{int(time.time()):08x}{secrets.token_hex(12)}"


def new_span_id() -> str:
    return secrets.token_hex(8)


def current_span_id() -> str:
    return _span.get()


def current_parent_span_id() -> str:
    return _parent.get()


def trace_id_from_traceparent(header_value: str | None) -> str | None:
    """W3C traceparent: "version-traceid-spanid-flags". Returns the trace id
    if it parses and looks real (right length, not all zeros) — otherwise
    None, so the caller generates a fresh one instead of propagating
    garbage a client happened to send."""
    if not header_value:
        return None
    parts = header_value.strip().split("-")
    if len(parts) != 4:
        return None
    trace_id = parts[1].lower()
    if not _TRACE_ID_RE.match(trace_id) or trace_id == "0" * 32:
        return None
    return trace_id


def current_trace_id() -> str:
    """Empty string outside of trace_context (e.g. a unit test calling a
    service method directly) — never raises, since a missing trace id
    should degrade a log line, not break the request."""
    return _current.get()


@contextmanager
def trace_context(traceparent_header: str | None) -> Iterator[str]:
    adopted = trace_id_from_traceparent(traceparent_header)
    trace_id = adopted or new_trace_id()
    parent = (traceparent_header or "").strip().lower().split("-")[2] if adopted else ""
    tokens = (
        (_current, _current.set(trace_id)),
        (_span, _span.set(new_span_id())),
        (_parent, _parent.set(parent if _SPAN_ID_RE.match(parent) else "")),
    )
    try:
        yield trace_id
    finally:
        for var, token in tokens:
            var.reset(token)
