#!/usr/bin/env python3
"""Daily close: compute commission from POS's confirmed-paid sales and write payout_ledger rows.

    COMMISSION_TENANT_IDS=t1,t2 python3 close.py               # close yesterday (EAT), every tenant
    python3 close.py --business-date 2026-09-20 --tenant t1    # close one day for one tenant

Safe to run more than once for the same day: `ledger/close.py::close_business_day` is idempotent
(a sale's commission is counted once no matter how many times its day is closed, and a day that
already has a live payout_ledger row is left alone). Wiring this to cron/EventBridge is
Platform's job (ADR 0006 open question 9); this script is what gets wired to whatever scheduler.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from ledger.close import close_business_day
from ledger.config import ConfigError, Settings
from ledger.pos_client import POSClient
from ledger.store import Store


def _yesterday(utc_offset_hours: int) -> date:
    now_local = datetime.now(timezone.utc) + timedelta(hours=utc_offset_hours)
    return (now_local - timedelta(days=1)).date()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--business-date", help="YYYY-MM-DD; defaults to yesterday in the configured offset"
    )
    parser.add_argument(
        "--tenant",
        action="append",
        dest="tenants",
        help="repeatable; defaults to COMMISSION_TENANT_IDS",
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"close: {exc}", file=sys.stderr)
        return 2

    tenants = args.tenants or [
        t.strip() for t in os.environ.get("COMMISSION_TENANT_IDS", "").split(",") if t.strip()
    ]
    if not tenants:
        print("close: no tenants given (--tenant or COMMISSION_TENANT_IDS)", file=sys.stderr)
        return 2

    business_day = (
        date.fromisoformat(args.business_date)
        if args.business_date
        else _yesterday(settings.business_day_utc_offset_hours)
    )

    store = Store(settings.db_path)
    pos_client = POSClient(settings.pos_base_url, settings.http_timeout_seconds)

    results = [
        close_business_day(store, pos_client, settings, tenant, business_day) for tenant in tenants
    ]
    summary = {
        "business_date": business_day.isoformat(),
        "tenants": results,
        "aborted": [r["tenant_id"] for r in results if r["aborted"]],
    }
    print(json.dumps(summary, sort_keys=True))
    return 1 if summary["aborted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
