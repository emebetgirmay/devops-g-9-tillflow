#!/usr/bin/env python3
"""Commission -> B2C: send every PLANNED payout_ledger row to Payments, then reconcile the ones
already REQUESTED. Run repeatedly (cron/EventBridge, Platform's to wire); each pass is safe to
overlap or repeat -- see ledger/disburse.py for why.

    python3 disburse.py
"""

from __future__ import annotations

import json
import sys

from ledger.config import ConfigError, Settings
from ledger.disburse import reconcile_requested_payouts, send_due_payouts
from ledger.store import Store


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"disburse: {exc}", file=sys.stderr)
        return 2

    store = Store(settings.db_path)
    sent = send_due_payouts(store, settings)
    reconciled = reconcile_requested_payouts(store, settings)
    summary = {"send": sent, "reconcile": reconciled}
    print(json.dumps(summary, sort_keys=True))
    return 1 if sent["aborted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
