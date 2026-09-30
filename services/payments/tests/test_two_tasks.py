"""Two Payments tasks on one database (what RDS makes possible, ADR 0002): the single writer rule
must hold across processes, not only across threads. Two independent App instances share a
database here and race the same create and the same callback. Runs on SQLite and on PostgreSQL
(TEST_POSTGRES_URL), where the rule is an advisory lock instead of BEGIN IMMEDIATE."""

from __future__ import annotations

import threading
import unittest

import _bootstrap  # noqa: F401
from helpers import KEY, ServiceTestCase, payment_body, payout_body

from app import App


class TwoTasksOneDatabaseTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        # A second task: its own App and connections, the same database, and the same provider
        # (two tasks talk to one M-Pesa; a success callback is confirmed by querying it).
        self.other = App(self.app.settings, clock=self.clock, adapter=self.adapter)
        self.apps = [self.app, self.other]

    def race(self, calls: list) -> list:
        """Run the callables at the same moment; return their results in call order."""
        results: list = [None] * len(calls)
        start = threading.Barrier(len(calls))

        def run(i: int) -> None:
            start.wait()
            results[i] = calls[i]()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def post(self, app: App, path: str, body: dict):
        raw = __import__("json").dumps(body).encode()
        return app.dispatch("POST", path, {"Idempotency-Key": KEY}, raw, "127.0.0.1")

    def test_the_same_create_on_both_tasks_makes_one_payment(self) -> None:
        calls = [lambda a=self.apps[i % 2]: self.post(a, "/payments", payment_body()) for i in range(12)]
        statuses = [reply.status for reply in self.race(calls)]
        self.assertEqual(statuses.count(201), 1)
        self.assertTrue(set(statuses) <= {200, 201, 409})
        self.assertEqual(self.count("payments"), 1)
        # One initiate reached the provider in total: nobody was charged twice.
        self.assertEqual(self.adapter.initiate_call_count, 1)

    def test_the_same_payout_on_both_tasks_makes_one_disbursement(self) -> None:
        calls = [lambda a=self.apps[i % 2]: self.post(a, "/payouts", payout_body()) for i in range(12)]
        statuses = [reply.status for reply in self.race(calls)]
        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(self.count("disbursements"), 1)
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_the_same_callback_on_both_tasks_credits_once(self) -> None:
        payment_id = self.pay().body["payment_id"]
        self.advance(2)
        (delivery,) = self.adapter.due_callbacks()
        path = "/payments/daraja/callback"
        calls = [
            lambda a=self.apps[i % 2]: a.dispatch("POST", path, delivery.headers, delivery.body, "127.0.0.1")
            for i in range(12)
        ]
        verdicts = [reply.body.get("status") for reply in self.race(calls)]
        self.assertEqual(verdicts.count("applied"), 1)
        self.assertEqual(verdicts.count("replay"), 11)
        self.assertEqual(self.payment(payment_id)["state"], "SUCCEEDED")
        self.assertEqual(self.count("ledger_entries"), 1)


if __name__ == "__main__":
    unittest.main()
