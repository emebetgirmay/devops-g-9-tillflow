"""A trace must explain a duplicate or reordered callback on its own (brief, G4 callback replay):
each delivery logs its verdict, and the anomaly for a reordered one carries the same trace id."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

import _bootstrap  # noqa: F401
from helpers import ServiceTestCase, key

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE}-00f067aa0ba902b7-01"


class TraceExplainsDuplicatesTest(ServiceTestCase):
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


if __name__ == "__main__":
    unittest.main()
