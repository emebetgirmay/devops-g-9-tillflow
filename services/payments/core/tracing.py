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
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_current: ContextVar[str] = ContextVar("trace_id", default="")


def new_trace_id() -> str:
    return secrets.token_hex(16)


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
    trace_id = trace_id_from_traceparent(traceparent_header) or new_trace_id()
    token = _current.set(trace_id)
    try:
        yield trace_id
    finally:
        _current.reset(token)
