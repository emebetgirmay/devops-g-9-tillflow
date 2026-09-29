#!/usr/bin/env python3
"""G4 Payments + integrity drills against a running Payments service (FakeAdapter only).

    PAYMENTS_URL=https://<api> python3 evidence/payments-integrity/g4/drills.py

Brief, "Prove failure and recovery": uncertain payment (force a timeout, keep it pending,
query/reconcile, prove a retry cannot charge again) and callback replay (replay and reorder,
one legal transition, one ledger effect, a trace that explains the duplicate). Every step is
timed twice: wall clock (what the drill took) and provider clock (the service's FakeAdapter
ManualClock, advanced with /_fake/advance, so "90 s with no callback" takes no real time).

Writes one JSON file per drill next to this script plus checks.json; exits 1 if any check fails.
Stdlib only. Never reaches Safaricom: /_fake/* answers 404 unless the service runs the fake.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("PAYMENTS_URL", "http://127.0.0.1:8080").rstrip("/")
OUT = Path(__file__).resolve().parent
RUN = time.strftime("%Y%m%dT%H%M%S", time.gmtime())

# Magic MSISDNs from services/_shared/mpesa (fake_adapter.py, fake_disbursement.py).
STK_TIMEOUT_QUERY_RESOLVES = "254000000007"
STK_INITIATE_TIMEOUT = "254000000012"
STK_DUPLICATE_CALLBACK = "254000000009"
STK_SUCCESS_THEN_FAILURE = "254000000010"
STK_FAILURE_THEN_SUCCESS = "254000000011"
B2C_TIMEOUT_QUERY_RESOLVES = "254000000107"
B2C_DUPLICATE_RESULT = "254000000109"
B2C_SUCCESS_THEN_FAILURE = "254000000110"

# Settings defaults (services/payments/core/config.py): callback deadline 90 s, reconcile SLA 120 s.
PAST_CALLBACK_DEADLINE_S = 100
PAST_RECONCILE_SLA_S = 130


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    """-> (status, json body, response headers). Never raises on an HTTP error status."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}"), dict(e.headers)
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")[:200]}, dict(e.headers)


def traceparent() -> str:
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


class Drill:
    """Records each step with wall and provider timestamps, relative to the drill's start."""

    def __init__(self, name: str) -> None:
        self.name, self.steps, self.checks = name, [], {}
        self.wall0 = time.monotonic()
        self.clock0 = self.provider_now()

    def provider_now(self) -> float:
        status, body, _ = call("POST", "/_fake/advance", {"seconds": 0})
        if status != 200:
            sys.exit(f"{BASE} is not running the FakeAdapter with a manual clock: {status} {body}")
        return body["now"]

    def step(self, label: str, status: int, body: dict, extra: dict | None = None) -> dict:
        self.steps.append(
            {
                "step": label,
                "http": status,
                "wall_s": round(time.monotonic() - self.wall0, 3),
                "provider_s": round(self.provider_now() - self.clock0, 3),
                "body": body,
                **(extra or {}),
            }
        )
        return body

    def advance(self, seconds: float) -> None:
        call("POST", "/_fake/advance", {"seconds": seconds})

    def check(self, name: str, ok: bool) -> None:
        self.checks[name] = bool(ok)

    def save(self, **extra) -> dict:
        result = {
            "drill": self.name,
            "run": RUN,
            "target": BASE,
            "wall_total_s": round(time.monotonic() - self.wall0, 3),
            "provider_total_s": round(self.provider_now() - self.clock0, 3),
            "checks": self.checks,
            "passed": all(self.checks.values()),
            **extra,
            "steps": self.steps,
        }
        (OUT / f"{self.name}.json").write_text(json.dumps(result, indent=2) + "\n")
        return result


def key(label: str) -> str:
    return f"g4-{RUN}-{label}"[:64]


def stk(msisdn: str, ref: str) -> dict:
    return {"tenant_id": "g4-drill", "msisdn": msisdn, "amount": 150000, "account_reference": ref}


def payout(msisdn: str, period: str) -> dict:
    return {
        "tenant_id": "g4-drill",
        "attendant_id": "att-g4",
        "payout_period": period,
        "msisdn": msisdn,
        "amount": 500000,
    }


def create(d: Drill, label: str, path: str, body: dict, idem: str) -> tuple[int, dict, dict]:
    s, reply, h = call("POST", path, body, {"Idempotency-Key": idem})
    d.step(label, s, reply, {"replayed": h.get("Idempotent-Replayed")})
    return s, reply, h


# Drill 1: uncertain STK payment -------------------------------------------------------------


def uncertain_payment() -> dict:
    d = Drill("uncertain-payment")
    k, body = key("stk-timeout"), stk(STK_TIMEOUT_QUERY_RESOLVES, "g4-timeout")
    _, created, _ = create(d, "create: provider accepts, callback never comes", "/payments", body, k)
    pid = created.get("payment_id", "")

    s, again, _ = create(d, "client retries with the same key while pending", "/payments", body, k)
    d.check("retry_while_pending_returns_original", s == 200 and again.get("payment_id") == pid)

    d.advance(PAST_CALLBACK_DEADLINE_S)
    s, sweep, _ = call("POST", "/_admin/sweep", {})
    d.step(f"no callback for {PAST_CALLBACK_DEADLINE_S}s: sweep", s, sweep)
    s, view, _ = call("GET", f"/payments/{pid}")
    d.step("state after sweep", s, view)
    d.check("timeout_is_unknown_not_declined", view.get("state") == "UNKNOWN")

    s, again, _ = create(d, "client retries with the same key while UNKNOWN", "/payments", body, k)
    d.check("retry_while_unknown_returns_original", s == 200 and again.get("payment_id") == pid)

    d.advance(PAST_RECONCILE_SLA_S)
    s, rec, _ = call("POST", f"/payments/{pid}/reconcile")
    d.step("reconcile by status query", s, rec)
    s, final, _ = call("GET", f"/payments/{pid}")
    d.step("final state", s, final)
    d.check("reconcile_resolves_to_succeeded", final.get("state") == "SUCCEEDED")
    d.check("exactly_one_ledger_entry", final.get("ledger_entries") == 1)

    s, _, _ = create(d, "client retries with the same key after recovery", "/payments", body, k)
    _, final2, _ = call("GET", f"/payments/{pid}")
    d.check("retry_after_recovery_changes_nothing", s == 200 and final2.get("ledger_entries") == 1)

    # The initiate call itself times out. The fake raises DuplicateInitiateError on a second
    # initiate for one key, so anything but a replayed 200 would mean an attempt to charge twice.
    k2, body2 = key("stk-init-timeout"), stk(STK_INITIATE_TIMEOUT, "g4-inittmo")
    _, c2, _ = create(d, "create: the initiate call itself times out", "/payments", body2, k2)
    d.check("initiate_timeout_is_unknown", c2.get("state") == "UNKNOWN")
    s, r2, _ = create(d, "client retries the timed-out initiate with the same key", "/payments", body2, k2)
    d.check("no_second_initiate_on_retry", s == 200 and r2.get("payment_id") == c2.get("payment_id"))
    return d.save()


# Drill 2: uncertain B2C payout --------------------------------------------------------------


def uncertain_payout() -> dict:
    d = Drill("uncertain-payout")
    k, body = key("b2c-timeout"), payout(B2C_TIMEOUT_QUERY_RESOLVES, f"g4-{RUN}-tmo")
    _, created, _ = create(d, "create: accepted, result never comes", "/payouts", body, k)
    did = created.get("disbursement_id", "")

    d.advance(PAST_CALLBACK_DEADLINE_S)
    s, sweep, _ = call("POST", "/_admin/sweep", {})
    d.step(f"no result for {PAST_CALLBACK_DEADLINE_S}s: sweep", s, sweep)
    s, view, _ = call("GET", f"/payouts/{did}")
    d.step("state after sweep", s, view)
    d.check("timeout_is_unknown_not_failed", view.get("state") == "UNKNOWN")

    s, again, _ = create(d, "Commission reruns with the same key", "/payouts", body, k)
    d.check("same_key_returns_original", s == 200 and again.get("disbursement_id") == did)
    _, other, _ = create(d, "a second key for the same payout", "/payouts", body, key("b2c-tmo-key2"))
    d.check("second_key_cannot_pay_again", other.get("error") == "payout_already_requested")

    d.advance(PAST_RECONCILE_SLA_S)
    s, rec, _ = call("POST", f"/payouts/{did}/reconcile")
    d.step("reconcile by OriginatorConversationID", s, rec)
    s, final, _ = call("GET", f"/payouts/{did}")
    d.step("final state", s, final)
    d.check("reconcile_resolves_to_succeeded", final.get("state") == "SUCCEEDED")
    d.check("exactly_one_ledger_entry", final.get("ledger_entries") == 1)
    return d.save()


# Drill 3: callback replay and reorder -------------------------------------------------------


def deliver(d: Drill, label: str, ref: str) -> tuple[list[str], str]:
    """Deliver due callbacks under a fresh trace id; returns the statuses for this provider
    reference (others, such as an earlier drill's late "unmatched" callback, stay in the
    evidence but are not this drill's) and the trace id."""
    s, body, h = call("POST", "/_fake/deliver-callbacks", {}, {"traceparent": traceparent()})
    trace_id = h.get("X-Trace-Id", "")
    d.step(label, s, body, {"trace_id": trace_id})
    return [x["status"] for x in body.get("delivered", []) if x.get("ref") == ref], trace_id


def callback_replay() -> dict:
    d = Drill("callback-replay")
    traces: dict[str, str] = {}

    _, dup, _ = create(d, "create: provider will send the same callback 3 times", "/payments",
                       stk(STK_DUPLICATE_CALLBACK, "g4-dup"), key("stk-dup"))
    d.advance(2)
    seen, traces["stk_duplicate"] = deliver(d, "deliver the 3 identical callbacks", dup.get("checkout_request_id"))
    s, v, _ = call("GET", f"/payments/{dup.get('payment_id')}")
    d.step("state after duplicates", s, v)
    d.check("stk_duplicates_applied_once", seen == ["applied", "replay", "replay"])
    d.check("stk_duplicate_one_ledger_entry", v.get("state") == "SUCCEEDED" and v.get("ledger_entries") == 1)

    _, sf, _ = create(d, "create: success arrives, then a contradicting failure", "/payments",
                      stk(STK_SUCCESS_THEN_FAILURE, "g4-sf"), key("stk-sf"))
    d.advance(30)
    seen, traces["stk_success_then_failure"] = deliver(d, "deliver success, then failure", sf.get("checkout_request_id"))
    s, v, _ = call("GET", f"/payments/{sf.get('payment_id')}")
    d.step("state after reorder", s, v)
    d.check("stk_one_legal_transition", seen.count("applied") == 1)
    d.check("stk_late_failure_cannot_undo_success", v.get("state") == "SUCCEEDED" and v.get("ledger_entries") == 1)

    _, fs, _ = create(d, "create: failure arrives, then a contradicting success", "/payments",
                      stk(STK_FAILURE_THEN_SUCCESS, "g4-fs"), key("stk-fs"))
    d.advance(30)
    seen, traces["stk_failure_then_success"] = deliver(d, "deliver failure, then success", fs.get("checkout_request_id"))
    s, v, _ = call("GET", f"/payments/{fs.get('payment_id')}")
    d.step("state after reorder", s, v)
    # Terminal states are immutable (ADR 0006): the contradicting success is logged as an anomaly
    # for a human and never credited automatically.
    d.check("stk_failure_then_success_one_transition", seen.count("applied") <= 1)
    d.check("stk_late_success_never_auto_credited", v.get("ledger_entries") == 0)

    _, pd, _ = create(d, "create payout: provider will send the same result 3 times", "/payouts",
                      payout(B2C_DUPLICATE_RESULT, f"g4-{RUN}-dup"), key("b2c-dup"))
    d.advance(2)
    seen, traces["b2c_duplicate"] = deliver(d, "deliver the 3 identical B2C results", pd.get("originator_conversation_id"))
    s, v, _ = call("GET", f"/payouts/{pd.get('disbursement_id')}")
    d.step("payout state after duplicates", s, v)
    d.check("b2c_duplicates_applied_once", seen == ["applied", "replay", "replay"])
    d.check("b2c_duplicate_one_ledger_entry", v.get("state") == "SUCCEEDED" and v.get("ledger_entries") == 1)

    _, ps, _ = create(d, "create payout: success result, then a contradicting failure", "/payouts",
                      payout(B2C_SUCCESS_THEN_FAILURE, f"g4-{RUN}-sf"), key("b2c-sf"))
    d.advance(30)
    seen, traces["b2c_success_then_failure"] = deliver(d, "deliver B2C success, then failure", ps.get("originator_conversation_id"))
    s, v, _ = call("GET", f"/payouts/{ps.get('disbursement_id')}")
    d.step("payout state after reorder", s, v)
    d.check("b2c_one_legal_transition", seen.count("applied") == 1)
    d.check("b2c_late_failure_cannot_undo_success", v.get("state") == "SUCCEEDED" and v.get("ledger_entries") == 1)
    return d.save(trace_ids=traces)


def main() -> int:
    results = [uncertain_payment(), uncertain_payout(), callback_replay()]
    summary = {
        "run": RUN,
        "target": BASE,
        "drills": {
            r["drill"]: {k: r[k] for k in ("passed", "wall_total_s", "provider_total_s", "checks")}
            for r in results
        },
        "trace_ids": results[2]["trace_ids"],
        "all_passed": all(r["passed"] for r in results),
    }
    (OUT / "checks.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
