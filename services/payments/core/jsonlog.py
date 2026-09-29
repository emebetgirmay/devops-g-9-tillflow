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
    print(json.dumps(line, sort_keys=True), flush=True)
