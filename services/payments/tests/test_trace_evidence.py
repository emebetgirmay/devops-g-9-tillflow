"""A trace must explain a duplicate or reordered callback on its own (brief, G4 callback replay):
each delivery logs its verdict, and the anomaly for a reordered one carries the same trace id."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

import sqlite3
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401
from helpers import ServiceTestCase, key, payment_body, payout_body

from core.store import Store

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE}-00f067aa0ba902b7-01"
SALE_TRACE = "0af7651916cd43dd8448eb211c80319c"
SALE_TRACEPARENT = f"00-{SALE_TRACE}-b7ad6b7169203331-01"


class TracedCase(ServiceTestCase):
    """Delivers the fake's due callbacks under TRACE and returns that trace's log lines."""

    def deliver_traced(self) -> list[dict]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.call("POST", "/_fake/deliver-callbacks", {}, {"traceparent": TRACEPARENT})
        lines = []
        for raw in (out.getvalue() + err.getvalue()).splitlines():
            try:
                lines.append(json.loads(raw))
            except ValueError:
                continue
        return [line for line in lines if line.get("trace_id") == TRACE]


class TraceExplainsDuplicatesTest(TracedCase):
    def test_duplicate_callbacks_log_applied_then_replays_with_one_transition(self) -> None:
        ref = self.pay(key(1), msisdn="254000000009").body["checkout_request_id"]
        self.advance(2)
        lines = self.deliver_traced()
        verdicts = [x["result"] for x in lines if x["event"] == "callback" and x["record_id"] == ref]
        self.assertEqual(verdicts, ["applied", "replay", "replay"])
        transitions = [x for x in lines if x["event"] == "state_transition"]
        self.assertEqual([x["state"] for x in transitions], ["SUCCEEDED"])

    def test_reordered_callback_anomaly_carries_the_trace_id(self) -> None:
        ref = self.pay(key(2), msisdn="254000000010").body["checkout_request_id"]
        self.advance(30)
        lines = self.deliver_traced()
        verdicts = [x["result"] for x in lines if x["event"] == "callback" and x["record_id"] == ref]
        self.assertEqual(verdicts, ["applied", "illegal_transition_logged"])
        self.assertTrue(
            any(x["event"] == "anomaly" and x["kind"] == "illegal_transition" for x in lines)
        )


class SaleTraceReachesTheCallbackTest(TracedCase):
    """One id finds the whole money path: the record remembers the trace that created it, and a
    later request that moves it (callback, sweep, reconcile) logs that id as origin_trace_id."""

    def create_traced(self, path: str, body: dict, idem: str) -> dict:
        headers = {"Idempotency-Key": idem, "traceparent": SALE_TRACEPARENT}
        return self.call("POST", path, body, headers).body

    def test_callback_transition_points_back_at_the_sale_trace(self) -> None:
        created = self.create_traced("/payments", payment_body(msisdn="254000000001"), key(3))
        self.advance(2)
        lines = self.deliver_traced()
        (moved,) = [x for x in lines if x["event"] == "state_transition"]
        self.assertEqual((moved["record_id"], moved["state"]), (created["payment_id"], "SUCCEEDED"))
        self.assertEqual((moved["trace_id"], moved["origin_trace_id"]), (TRACE, SALE_TRACE))

    def test_payout_result_transition_points_back_at_the_run_trace(self) -> None:
        created = self.create_traced("/payouts", payout_body(msisdn="254000000101"), key(4))
        self.advance(2)
        lines = self.deliver_traced()
        (moved,) = [x for x in lines if x["event"] == "state_transition"]
        self.assertEqual(moved["record_id"], created["disbursement_id"])
        self.assertEqual(moved["origin_trace_id"], SALE_TRACE)

    def test_a_transition_in_the_creating_request_has_no_origin_field(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.create_traced("/payments", payment_body(msisdn="254000000001"), key(5))
        lines = [json.loads(raw) for raw in out.getvalue().splitlines() if raw.startswith("{")]
        (moved,) = [x for x in lines if x["event"] == "state_transition"]
        self.assertEqual(moved["trace_id"], SALE_TRACE)
        self.assertNotIn("origin_trace_id", moved)

    def test_callback_line_carries_the_provider_code(self) -> None:
        ref = self.pay(key(6), msisdn="254000000001").body["checkout_request_id"]
        self.advance(2)
        (line,) = [x for x in self.deliver_traced() if x["event"] == "callback"]
        self.assertEqual((line["record_id"], line["result"], line["code"]), (ref, "applied", "0"))


class OlderDatabaseFileTest(unittest.TestCase):
    def test_trace_id_column_is_added_to_an_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            Store(path)
            conn = sqlite3.connect(path)
            for table in ("payments", "disbursements"):
                conn.execute(f"ALTER TABLE {table} DROP COLUMN trace_id")
            conn.commit()
            conn.close()
            Store(path)
            conn = sqlite3.connect(path)
            for table in ("payments", "disbursements"):
                columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                self.assertIn("trace_id", columns)
            conn.close()


if __name__ == "__main__":
    unittest.main()
