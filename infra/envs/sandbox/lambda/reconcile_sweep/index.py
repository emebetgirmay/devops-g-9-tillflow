"""Scheduled reconcile pass (ADR 0009 G3-10). EventBridge Scheduler runs it every 5 minutes.

Each Payments task keeps its own SQLite database, so the pass must run inside the service: this
calls `POST /_admin/sweep` on the internal ALB, which routes `/_admin*` to Payments. A failed pass
raises, so the invocation counts as an error in Lambda metrics, and Payments'
`payments_reconcile_runs_total{result="error"}` records it on its side; `reconcile-stale` fires if
no pass succeeds for 15 minutes.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


class SweepFailed(Exception):
    pass


def sweep(url: str, timeout: float = 50.0) -> dict:
    req = urllib.request.Request(
        url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (internal ALB)
            status = resp.status
            body = resp.read(65536)
    except urllib.error.HTTPError as exc:
        raise SweepFailed(f"http {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SweepFailed(f"error {type(exc).__name__}") from exc
    if status != 200:
        raise SweepFailed(f"http {status}")
    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        result = {}
    return {"status": status, "seconds": round(time.monotonic() - started, 2), "result": result}


def handler(_event, _context, run=sweep):
    try:
        out = run(os.environ["SWEEP_URL"])
    except SweepFailed as exc:
        print(json.dumps({"event": "reconcile_sweep_failed", "detail": str(exc)}))
        raise
    # Counts only: the sweep result holds no ids worth logging here, and never phone numbers.
    print(json.dumps({"event": "reconcile_sweep_ok", "status": out["status"], "seconds": out["seconds"]}))
    return {"ok": True, "seconds": out["seconds"]}
