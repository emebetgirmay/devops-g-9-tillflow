"""Request trace id for POS, the same W3C ``traceparent`` contract Payments and Commission use
(ADR 0009 section 5): take the caller's trace id or start one, log it on the request line, return
it as ``X-Trace-Id``, and pass it to Payments, so one id follows a sale into its payment.

A ContextVar, not a global: requests run concurrently.
"""

from __future__ import annotations

import re
import secrets
import time
from contextvars import ContextVar

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_current: ContextVar[str] = ContextVar("trace_id", default="")
# This request's own span, and the caller's span it hangs under (empty for a trace we started).
_span: ContextVar[str] = ContextVar("span_id", default="")
_parent: ContextVar[str] = ContextVar("parent_span_id", default="")


def start(traceparent: str | None) -> str:
    """Adopt the trace id of a well-formed ``traceparent``, else start a new one."""
    parts = (traceparent or "").strip().lower().split("-")
    trace_id, parent = (parts[1], parts[2]) if len(parts) == 4 else ("", "")
    if not _TRACE_ID_RE.match(trace_id) or trace_id == "0" * 32:
        # The first 8 hex are the epoch second, the form X-Ray's own trace ids take.
        trace_id, parent = f"{int(time.time()):08x}{secrets.token_hex(12)}", ""
    _current.set(trace_id)
    _span.set(secrets.token_hex(8))
    _parent.set(parent if _SPAN_ID_RE.match(parent) else "")
    return trace_id


def span_id() -> str:
    return _span.get()


def parent_span_id() -> str:
    return _parent.get()


def traceparent() -> str | None:
    """Header for an outbound call, naming this request's span as the parent so the callee's span
    nests under it. None outside a request (the background reconcile loop)."""
    trace_id = _current.get()
    return f"00-{trace_id}-{_span.get()}-01" if trace_id else None
