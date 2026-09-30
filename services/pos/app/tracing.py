"""Request trace id for POS, the same W3C ``traceparent`` contract Payments and Commission use
(ADR 0009 section 5): take the caller's trace id or start one, log it on the request line, return
it as ``X-Trace-Id``, and pass it to Payments, so one id follows a sale into its payment.

A ContextVar, not a global: requests run concurrently.
"""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_current: ContextVar[str] = ContextVar("trace_id", default="")


def start(traceparent: str | None) -> str:
    """Adopt the trace id of a well-formed ``traceparent``, else start a new one."""
    parts = (traceparent or "").strip().lower().split("-")
    trace_id = parts[1] if len(parts) == 4 else ""
    if not _TRACE_ID_RE.match(trace_id) or trace_id == "0" * 32:
        trace_id = secrets.token_hex(16)
    _current.set(trace_id)
    return trace_id


def traceparent() -> str | None:
    """Header for an outbound call, or None outside a request (the background reconcile loop)."""
    trace_id = _current.get()
    return f"00-{trace_id}-{secrets.token_hex(8)}-01" if trace_id else None
