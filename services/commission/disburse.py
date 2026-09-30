#!/usr/bin/env python3
"""Commission -> B2C: send every PLANNED payout_ledger row to Payments, then reconcile the ones
already REQUESTED. Run repeatedly (cron/EventBridge, Platform's to wire); each pass is safe to
overlap or repeat -- see ledger/disburse.py for why.

    python3 disburse.py            send and reconcile
    python3 disburse.py --check    read only: print {"event": "payouts_not_terminal", "count": N}

--check is the 06:30 EAT SLO probe (docs/slo-error-budgets.md, Commission). It sends and
reconciles nothing; N counts payout_ledger rows still PLANNED or REQUESTED, 0 when all are
terminal. A CloudWatch log metric filter turns the line into the alarm, so no AWS SDK is needed.
"""

from __future__ import annotations

import argparse
import json
import sys

from ledger import tracing
from ledger.config import ConfigError, Settings
from ledger.disburse import reconcile_requested_payouts, send_due_payouts
from ledger.store import Store


NOT_TERMINAL = ("PLANNED", "REQUESTED")


def count_not_terminal(store: Store) -> int:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM payout_ledger WHERE state IN (?, ?)", NOT_TERMINAL
        ).fetchone()
    return int(row["n"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--check", action="store_true", help="count payouts not terminal; send nothing"
    )
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"disburse: {exc}", file=sys.stderr)
        return 2

    if args.check:
        # One line on stdout, exactly what the metric filter matches. Exit 0 whatever N is: the
        # alarm is the signal, and a failed task is its own, separate signal.
        count = count_not_terminal(Store(settings.database_url or settings.db_path))
        print(json.dumps({"event": "payouts_not_terminal", "count": count}), flush=True)
        return 0

    trace_id = tracing.start_new_run()
    print(json.dumps({"event": "disburse_run_started", "trace_id": trace_id}), file=sys.stderr)

    store = Store(settings.database_url or settings.db_path)
    sent = send_due_payouts(store, settings)
    reconciled = reconcile_requested_payouts(store, settings)
    summary = {"send": sent, "reconcile": reconciled}
    print(json.dumps(summary, sort_keys=True))
    return 1 if sent["aborted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
