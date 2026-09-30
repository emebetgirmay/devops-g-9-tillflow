"""One trace id per Commission run (ADR 0009 section 5): sent to Payments as
a W3C ``traceparent`` header on every call, so a single ``close.py``/
``disburse.py`` invocation can be followed end to end through Payments' own
state-transition log lines (core/tracing.py + core/jsonlog.py on that side).

Much simpler than Payments' own core/tracing.py: this is a single-threaded
batch script invoked once per cron/EventBridge tick, not a threaded HTTP
server handling concurrent requests, so a plain module-level value is
enough — no contextvars needed.
"""

from __future__ import annotations

import secrets
import time

_trace_id: str | None = None


def _new_trace_id() -> str:
    """Time-based (8 hex digits of Unix time, then 24 random): X-Ray drops spans whose trace id
    does not start with the current time, and Payments records spans under this id."""
    return f"{int(time.time()):08x}{secrets.token_hex(12)}"


def start_new_run() -> str:
    """Call once at the top of a script's main() — a fresh trace id for
    this run, distinct from whatever the previous run (or an import in a
    long-lived test process) left behind."""
    global _trace_id
    _trace_id = _new_trace_id()
    return _trace_id


def current_trace_id() -> str:
    global _trace_id
    if _trace_id is None:
        _trace_id = _new_trace_id()
    return _trace_id


def traceparent_header() -> str:
    """W3C traceparent: version-traceid-spanid-flags. The span id is fresh
    per call (each is a distinct outbound request), the trace id is this
    run's, so Payments' side re-associates every call from one run under
    one trace_id."""
    return f"00-{current_trace_id()}-{secrets.token_hex(8)}-01"
