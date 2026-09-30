#!/usr/bin/env python3
"""Does Payments' money state survive the task being replaced? (ADR 0002)

On SQLite inside the container it did not: a redeploy forgot idempotency keys, payout keys and
the kill switch. On PostgreSQL it must. Run against a service on the FakeAdapter, in two phases
around a restart of the task:

    PAYMENTS_URL=http://127.0.0.1:8080 python3 restart.py before
    # replace the task: stop the container and start a new one on the same DATABASE_URL
    PAYMENTS_URL=http://127.0.0.1:8080 python3 restart.py after

`before` writes restart-state.json; `after` writes restart-checks.json and exits 1 on a failure.
Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("PAYMENTS_URL", "http://127.0.0.1:8080").rstrip("/")
OUT = Path(__file__).resolve().parent
STATE = OUT / "restart-state.json"


def call(method: str, path: str, body: dict | None = None, key: str | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Idempotency-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def stk(msisdn: str, ref: str) -> dict:
    return {"tenant_id": "restart", "msisdn": msisdn, "amount": 150000, "account_reference": ref}


def payout(msisdn: str, period: str) -> dict:
    return {"tenant_id": "restart", "attendant_id": "att-r", "payout_period": period,
            "msisdn": msisdn, "amount": 500000}


def before() -> int:
    run = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    keys = {name: f"restart-{run}-{name}" for name in ("paid", "pending", "payout", "funds")}
    _, paid = call("POST", "/payments", stk("254000000001", "r-paid"), keys["paid"])
    _, pending = call("POST", "/payments", stk("254000000006", "r-pending"), keys["pending"])
    _, paid_out = call("POST", "/payouts", payout("254000000101", f"{run}-ok"), keys["payout"])
    call("POST", "/_fake/advance", {"seconds": 2})
    call("POST", "/_fake/deliver-callbacks", {})
    # Insufficient funds (254000000102) is a definitive failure that trips the payouts kill switch.
    call("POST", "/payouts", payout("254000000102", f"{run}-funds"), keys["funds"])
    call("POST", "/_fake/advance", {"seconds": 2})
    call("POST", "/_fake/deliver-callbacks", {})
    s, blocked = call("POST", "/payouts", payout("254000000101", f"{run}-blocked"), f"restart-{run}-blocked")
    state = {
        "run": run, "keys": keys, "stopped_at": time.time(),
        "paid": call("GET", f"/payments/{paid['payment_id']}")[1],
        "pending": call("GET", f"/payments/{pending['payment_id']}")[1],
        "payout": call("GET", f"/payouts/{paid_out['disbursement_id']}")[1],
        "kill_switch_before": {"http": s, **blocked},
    }
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    summary = {k: state[k].get("state") for k in ("paid", "pending", "payout")}
    print(json.dumps({**summary, "kill_switch": blocked.get("error")}, indent=2))
    return 0


def after() -> int:
    was = json.loads(STATE.read_text())
    run, keys = was["run"], was["keys"]
    s_paid, paid = call("GET", f"/payments/{was['paid']['payment_id']}")
    s_out, paid_out = call("GET", f"/payouts/{was['payout']['disbursement_id']}")
    s_retry, retry = call("POST", "/payments", stk("254000000001", "r-paid"), keys["paid"])
    # The kill switch is on (that is what must survive), and it is checked before the payout key,
    # so a new key would get 503 either way. Replays are answered first: the original key must
    # still return the original payout.
    s_replay, replay = call("POST", "/payouts", payout("254000000101", f"{run}-ok"), keys["payout"])
    s_block, blocked = call("POST", "/payouts", payout("254000000101", f"{run}-after"), f"restart-{run}-after")
    # The payment that was PENDING when the task died: the new task's sweep must find it. The
    # fake's manual clock restarts with the process, so move well past where the old one had got
    # to (a real clock never goes back), but stay inside the 24 h reconcile window.
    call("POST", "/_fake/advance", {"seconds": 6 * 3600})
    call("POST", "/_admin/sweep", {})
    _, pending = call("GET", f"/payments/{was['pending']['payment_id']}")
    _, invariants = call("GET", "/_admin/invariants")
    checks = {
        "succeeded_payment_survived_with_its_ledger_entry": (
            s_paid == 200 and paid.get("state") == "SUCCEEDED" and paid.get("ledger_entries") == 1
            and paid.get("receipt") == was["paid"].get("receipt")
        ),
        "succeeded_payout_survived_with_its_ledger_entry": (
            s_out == 200 and paid_out.get("state") == "SUCCEEDED" and paid_out.get("ledger_entries") == 1
        ),
        "idempotency_key_survived_retry_returns_the_original": (
            s_retry == 200 and retry.get("payment_id") == was["paid"]["payment_id"]
        ),
        "payout_idempotency_survived_same_key_returns_the_original": (
            s_replay == 200 and replay.get("disbursement_id") == was["payout"]["disbursement_id"]
        ),
        "kill_switch_survived": s_block == 503 and blocked.get("error") == "payouts_disabled",
        "in_flight_payment_is_unknown_not_declined": pending.get("state") == "UNKNOWN",
        "invariants_hold": (
            invariants.get("credits_equal_succeeded_payments") is True
            and invariants.get("duplicate_ledger_entries") == 0
            and invariants.get("payout_keys_with_multiple_live_disbursements") == 0
        ),
    }
    result = {
        "run": run, "target": BASE,
        "task_down_for_s": round(time.time() - was["stopped_at"], 1),
        "checks": checks, "all_passed": all(checks.values()),
        "after": {"paid": paid, "payout": paid_out, "retry_http": s_retry, "payout_replay_http": s_replay,
                  "kill_switch": blocked, "pending": pending, "invariants": invariants},
    }
    (OUT / "restart-checks.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ("task_down_for_s", "checks", "all_passed")}, indent=2))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else ""
    if phase not in ("before", "after"):
        sys.exit(__doc__)
    raise SystemExit(before() if phase == "before" else after())
