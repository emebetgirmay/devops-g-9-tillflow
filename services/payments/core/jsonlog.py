"""Structured JSON logs to stdout: one line per request, one line per state
change (ADR 0009 section 5).

Separate from Store.anomaly()'s existing JSON lines to stderr, which are
unchanged and keep their own shape (they existed before this ADR and are
about illegal transitions and constraint violations specifically, not the
general "what happened" trail this module writes). Every line here carries
``trace_id`` (core/tracing.py) so one request — or, via Commission's
propagated ``traceparent``, one payout run — can be followed end to end.

No bodies, no phone numbers, no credentials, no balances: nothing that
calls this passes any of those in, so there's nothing to accidentally leak.
"""

from __future__ import annotations

import json
import time


def log_line(
    *,
    level: str,
    service: str,
    event: str,
    trace_id: str,
    record_kind: str | None = None,
    record_id: str | None = None,
    state: str | None = None,
    result: str | None = None,
    origin_trace_id: str | None = None,
    code: str | None = None,
) -> None:
    line = {
        "ts": time.time(),
        "level": level,
        "service": service,
        "event": event,
        "trace_id": trace_id,
        "record_kind": record_kind,
        "record_id": record_id,
        "state": state,
        "result": result,
    }
    # Only when set: the trace that created the record (a callback, sweep or reconcile moving it
    # later runs under its own trace id), and the provider's raw result code on a callback.
    if origin_trace_id:
        line["origin_trace_id"] = origin_trace_id
    if code is not None:
        line["code"] = code
    print(json.dumps(line, sort_keys=True), flush=True)
