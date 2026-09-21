#!/usr/bin/env python3
"""Commission worker (G2): ask the Payments API to pay each payout that is due.

Commission never calls Daraja and never imports the M-Pesa adapter: its only path to moving money
is POST /payouts on the Payments service (ADR 0004, ADR 0008). Amounts and recipients are supplied
by a stubbed input (a CSV, already aggregated); computing commission from sales is Product's work
and is not built here.

Input CSV columns: tenant_id, attendant_id, payout_period, msisdn, amount (integer minor units).

Safety properties:
- the Idempotency-Key is derived from (tenant, attendant, period), so running the worker again,
  or twice at once, asks for the same payout and Payments answers with the original;
- the worker never resubmits on its own: an unclear result is left for the next run, which is safe
  because the key is the same;
- a paused payouts switch (503) or an unreachable Payments service stops the run.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REQUIRED_COLUMNS = ("tenant_id", "attendant_id", "payout_period", "msisdn", "amount")
EXIT_OK, EXIT_PROBLEMS, EXIT_BAD_INPUT, EXIT_ABORTED = 0, 1, 2, 3

Post = Callable[[str, str, dict], tuple[int, dict]]


@dataclass(frozen=True)
class Entry:
    tenant_id: str
    attendant_id: str
    payout_period: str
    msisdn: str
    amount: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.tenant_id, self.attendant_id, self.payout_period)


def idempotency_key_for(tenant_id: str, attendant_id: str, payout_period: str) -> str:
    """Deterministic, 43 characters of [A-Za-z0-9_]: the same payout always gets the same key."""
    digest = hashlib.sha256(f"{tenant_id}|{attendant_id}|{payout_period}".encode()).hexdigest()
    return "po_" + digest[:40]


def read_entries(path: str | Path) -> list[Entry]:
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"input is missing columns: {', '.join(missing)}")
        entries = []
        for number, row in enumerate(reader, start=2):
            try:
                amount = int(row["amount"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"line {number}: amount must be an integer in minor units"
                ) from exc
            values = {name: (row[name] or "").strip() for name in REQUIRED_COLUMNS[:4]}
            if not all(values.values()):
                raise ValueError(
                    f"line {number}: tenant_id, attendant_id, payout_period, msisdn required"
                )
            entries.append(Entry(amount=amount, **values))
    return entries


def post_payout(base_url: str, key: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/payouts",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Idempotency-Key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        except ValueError:
            return error.code, {}


def run(entries: list[Entry], base_url: str, post: Post = post_payout) -> dict:
    """Request each distinct payout once. Returns a summary; never raises on a bad answer."""
    results: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    aborted: str | None = None
    for entry in entries:
        if entry.key in seen:
            results.append({"payout": list(entry.key), "outcome": "duplicate_in_input"})
            continue
        seen.add(entry.key)
        body = {
            "tenant_id": entry.tenant_id,
            "attendant_id": entry.attendant_id,
            "payout_period": entry.payout_period,
            "msisdn": entry.msisdn,
            "amount": entry.amount,
        }
        try:
            status, reply = post(base_url, idempotency_key_for(*entry.key), body)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            aborted = f"payments_unreachable: {exc}"
            results.append({"payout": list(entry.key), "outcome": "not_sent_unreachable"})
            break
        outcome = classify(status, reply)
        results.append({"payout": list(entry.key), "outcome": outcome, "http": status})
        if outcome == "payouts_disabled":
            aborted = "payouts_disabled"
            break
    counts: dict[str, int] = {}
    for item in results:
        counts[item["outcome"]] = counts.get(item["outcome"], 0) + 1
    return {"requested": len(seen), "counts": counts, "aborted": aborted, "results": results}


def classify(status: int, reply: dict) -> str:
    error = reply.get("error", "")
    if status == 201:
        return "created"
    if status == 200:
        return "already_requested"
    if status == 409 and error == "payout_already_requested":
        return "already_requested"
    if status == 409 and error == "idempotency_in_flight":
        return "in_flight_retry_next_run"
    if status == 409:
        return "conflict_needs_review"
    if status == 422:
        return f"held_{error}"
    if status == 503 and error == "payouts_disabled":
        return "payouts_disabled"
    return f"rejected_{status}"


def exit_code(summary: dict) -> int:
    if summary["aborted"]:
        return EXIT_ABORTED
    bad = [o for o in summary["counts"] if o.startswith(("conflict", "rejected"))]
    return EXIT_PROBLEMS if bad else EXIT_OK


def main() -> int:
    path = os.environ.get("COMMISSION_INPUT", "")
    base_url = os.environ.get("PAYMENTS_URL", "http://127.0.0.1:8080")
    if not path:
        print("commission: set COMMISSION_INPUT to a CSV file", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        entries = read_entries(path)
    except (OSError, ValueError) as exc:
        print(f"commission: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    summary = run(entries, base_url)
    print(json.dumps(summary, sort_keys=True))
    return exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
