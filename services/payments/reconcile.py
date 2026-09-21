#!/usr/bin/env python3
"""Reconcile pass: move lost attempts to UNKNOWN, then query every due UNKNOWN payment or payout.

    python3 reconcile.py                       run the pass in this process (uses DATABASE_URL)
    python3 reconcile.py --service-url URL     ask a running service to run it (POST /_admin/sweep)

It is idempotent and safe to run repeatedly (cron or EventBridge later; wiring is Platform's).
Rows are picked up when UNKNOWN for at least RECONCILE_SLA_SECONDS (default 120) and due by their
backoff. A payment is never auto-failed: after the window it becomes NEEDS_REVIEW.

Limitation of this build: the FakeAdapter keeps its state in memory, so an in-process pass in a
fresh process cannot resolve references issued by another process. Use --service-url against the
running service, or run the pass in the same process as the tests.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

_SHARED = Path(__file__).resolve().parent.parent / "_shared"
if _SHARED.is_dir():
    sys.path.insert(0, str(_SHARED))

from app import App
from core.config import ConfigError, Settings


def run_in_process(settings: Settings) -> dict:
    app = App(settings)
    return {"payments": app.payments.sweep(), "payouts": app.payouts.sweep()}


def run_via_service(url: str) -> dict:
    request = urllib.request.Request(url.rstrip("/") + "/_admin/sweep", data=b"{}", method="POST")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--service-url", help="base URL of a running payments service")
    args = parser.parse_args(argv)
    try:
        result = (
            run_via_service(args.service_url)
            if args.service_url
            else run_in_process(Settings.from_env())
        )
    except ConfigError as exc:
        print(f"reconcile: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
