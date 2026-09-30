"""Restore, then reconcile (runbook "Restore", step 5; brief: "reconcile provider references
before declaring recovery").

A point-in-time restore brings back the database as it was at T. A payment or payout that
completed after T is unfinished again in the restored data, but the provider still knows what
happened. This proves the recovery rule: a Payments service started on the restored data moves
those records to UNKNOWN and asks the provider, gets the real outcome, writes exactly one ledger
entry, and never sends the charge or the payout a second time.

The "restore" here copies every row into a fresh database (SQLite file or PostgreSQL schema); the
provider is one FakeAdapter shared by both services, as Daraja would be.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401
from helpers import ServiceTestCase, key

from app import App

TABLES = ("idempotency_keys", "payments", "disbursements", "ledger_entries", "outbox",
          "provider_callbacks", "unmatched_callbacks", "anomalies", "flags")


class RestoreThenReconcileTest(ServiceTestCase):
    def restore_as_of_now(self) -> App:
        """A second Payments service on a copy of the data as it is right now."""
        settings = replace(self.app.settings, db_path=str(Path(self._dir.name) / "restored.db"))
        restored = App(settings, clock=self.clock, adapter=self.adapter)
        with self.app.store.connection() as source, restored.store.tx() as target:
            for table in TABLES:
                for row in source.execute(f"SELECT * FROM {table}").fetchall():
                    # Surrogate ids are regenerated so the new database's sequences stay ahead.
                    columns = [c for c in dict(row) if not (c == "id" and table != "payments")]
                    target.execute(
                        f"INSERT INTO {table} ({', '.join(columns)})"
                        f" VALUES ({', '.join('?' for _ in columns)})",
                        [row[c] for c in columns],
                    )
        return restored

    def test_work_that_finished_after_the_restore_point_is_recovered_from_the_provider(self) -> None:
        payment_id = self.pay(key(1), msisdn="254000000001").body["payment_id"]
        payout_id = self.payout(key(2), msisdn="254000000101").body["disbursement_id"]

        restored = self.restore_as_of_now()  # T: both are still PENDING

        self.advance(2)
        self.deliver()  # after T, on the live database: both complete
        self.assertEqual(self.payment(payment_id)["state"], "SUCCEEDED")
        self.assertEqual(self.disbursement(payout_id)["state"], "SUCCEEDED")

        def get(path: str) -> dict:
            return restored.dispatch("GET", path, {}, b"", "127.0.0.1").body

        # The restored data does not know: nothing may be assumed paid or failed.
        self.assertEqual(get(f"/payments/{payment_id}")["state"], "PENDING")
        self.assertEqual(get(f"/payouts/{payout_id}")["state"], "PENDING")
        self.assertEqual(get(f"/payments/{payment_id}")["ledger_entries"], 0)

        charges, sends = self.adapter.initiate_call_count, self.adapter.disburse_call_count
        self.advance(100)  # past the callback deadline: the callbacks were lost with the restore
        restored.dispatch("POST", "/_admin/sweep", {}, b"{}", "127.0.0.1")
        self.assertEqual(get(f"/payments/{payment_id}")["state"], "UNKNOWN")
        self.assertEqual(get(f"/payouts/{payout_id}")["state"], "UNKNOWN")

        self.advance(130)  # past the reconcile SLA: the sweep now asks the provider
        restored.dispatch("POST", "/_admin/sweep", {}, b"{}", "127.0.0.1")
        payment, paid_out = get(f"/payments/{payment_id}"), get(f"/payouts/{payout_id}")
        self.assertEqual((payment["state"], payment["ledger_entries"]), ("SUCCEEDED", 1))
        self.assertEqual((paid_out["state"], paid_out["ledger_entries"]), ("SUCCEEDED", 1))

        # Recovered by asking, never by sending again.
        self.assertEqual(self.adapter.initiate_call_count, charges)
        self.assertEqual(self.adapter.disburse_call_count, sends)
        invariants = get("/_admin/invariants")
        self.assertTrue(invariants["credits_equal_succeeded_payments"])
        self.assertEqual(invariants["duplicate_ledger_entries"], 0)
        self.assertEqual(invariants["payout_keys_with_multiple_live_disbursements"], 0)

    def test_a_retry_after_the_restore_returns_the_original_not_a_new_charge(self) -> None:
        first = self.pay(key(3), msisdn="254000000001")
        restored = self.restore_as_of_now()
        body = __import__("json").dumps(
            {"tenant_id": "tenant-a", "msisdn": "254000000001", "amount": 150_000,
             "account_reference": "ref1"}
        ).encode()
        again = restored.dispatch("POST", "/payments", {"Idempotency-Key": key(3)}, body, "127.0.0.1")
        self.assertEqual((again.status, again.body["payment_id"]), (200, first.body["payment_id"]))
        self.assertEqual(self.adapter.initiate_call_count, 1)


if __name__ == "__main__":
    unittest.main()
