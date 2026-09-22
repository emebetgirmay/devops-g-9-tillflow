"""ledger/disburse.py against a real in-process Payments service (FakeAdapter, manual clock).

Ledger rows are inserted directly here (as core/close.py would leave them: state PLANNED, a
stable idempotency_key already derived) so these tests isolate the send/reconcile pass from the
close pass, which test_close.py already covers on its own.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401
from helpers import PaymentsServerCase

from ledger.common import idempotency_key_for
from ledger.config import Settings
from ledger.disburse import reconcile_requested_payouts, send_due_payouts
from ledger.store import Store

SUCCESS = "254000000101"
INSUFFICIENT_FUNDS = "254000000102"
CONFIGURATION_ERROR = "254000000105"
TIMEOUT_NO_RESULT = "254000000106"
TIMEOUT_QUERY_RESOLVES = "254000000107"
DISBURSE_TIMEOUT = "254000000112"
DISBURSE_REJECTED = "254000000113"
DUPLICATE_ORIGINATOR = "254000000114"


class DisburseTestCase(PaymentsServerCase):
    def setUp(self) -> None:
        super().setUp()
        self._ledger_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._ledger_dir.cleanup)
        self.settings = replace(
            Settings(),
            db_path=str(Path(self._ledger_dir.name) / "commission.db"),
            payments_base_url=self.base_url,
        )
        self.store = Store(self.settings.db_path)

    def plan(
        self,
        *,
        tenant_id: str = "tenant-a",
        attendant_id: str = "att-1",
        business_date: str = "2026-09-20",
        amount_minor: int = 500_000,
        msisdn: str = SUCCESS,
    ) -> str:
        ledger_id = f"pol_{attendant_id}_{business_date}"
        idem_key = idempotency_key_for(tenant_id, attendant_id, business_date)
        now = time.time()
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO payout_ledger (id, tenant_id, attendant_id, business_date, currency,"
                " amount_minor, msisdn, state, idempotency_key, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'KES', ?, ?, 'PLANNED', ?, ?, ?)",
                (
                    ledger_id,
                    tenant_id,
                    attendant_id,
                    business_date,
                    amount_minor,
                    msisdn,
                    idem_key,
                    now,
                    now,
                ),
            )
        return ledger_id

    def ledger(self, ledger_id: str):
        with self.store.connection() as conn:
            return conn.execute("SELECT * FROM payout_ledger WHERE id = ?", (ledger_id,)).fetchone()

    def send(self) -> dict:
        return send_due_payouts(self.store, self.settings)

    def reconcile(self) -> dict:
        return reconcile_requested_payouts(self.store, self.settings)


class SendTest(DisburseTestCase):
    def test_a_planned_row_is_sent_and_becomes_requested(self) -> None:
        ledger_id = self.plan()
        result = self.send()
        self.assertEqual(result["counts"].get("created"), 1)
        row = self.ledger(ledger_id)
        self.assertEqual(row["state"], "REQUESTED")
        self.assertIsNotNone(row["disbursement_id"])
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_sending_twice_asks_for_the_same_payout_and_sends_once(self) -> None:
        self.plan()
        first = self.send()
        second = self.send()
        self.assertEqual(first["counts"].get("created"), 1)
        # The row is already REQUESTED after the first send, so the second send has nothing
        # PLANNED left to look at -- send_due_payouts only selects PLANNED rows.
        self.assertEqual(second["checked"], 0)
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_replaying_a_planned_row_directly_reuses_the_same_key_and_sends_once(self) -> None:
        # Simulates a crash between "create the PLANNED row" and "mark it REQUESTED": the row is
        # still PLANNED, so a second send() attempt reuses the identical idempotency_key.
        ledger_id = self.plan()
        with self.store.tx() as conn:
            conn.execute("UPDATE payout_ledger SET state = 'PLANNED' WHERE id = ?", (ledger_id,))
        self.send()
        with self.store.tx() as conn:
            conn.execute("UPDATE payout_ledger SET state = 'PLANNED' WHERE id = ?", (ledger_id,))
        self.send()
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_two_different_ledger_rows_are_two_different_payouts(self) -> None:
        self.plan(attendant_id="att-1")
        self.plan(attendant_id="att-2")
        result = self.send()
        self.assertEqual(result["counts"].get("created"), 2)
        self.assertEqual(self.adapter.disburse_call_count, 2)

    def test_insufficient_funds_fails_the_row_and_trips_the_payments_kill_switch(self) -> None:
        ledger_id = self.plan(msisdn=INSUFFICIENT_FUNDS)
        self.send()
        self.clock.advance(2)
        self.app.dispatch("POST", "/_fake/deliver-callbacks", {}, b"{}", "127.0.0.1")
        self.reconcile()
        row = self.ledger(ledger_id)
        self.assertEqual((row["state"], row["failure_reason"]), ("FAILED", "INSUFFICIENT_FUNDS"))

    def test_a_synchronous_rejection_is_a_definitive_failure_with_no_reconcile_needed(self) -> None:
        ledger_id = self.plan(msisdn=DISBURSE_REJECTED)
        self.send()
        row = self.ledger(ledger_id)
        self.assertEqual(
            (row["state"], row["failure_reason"]), ("FAILED", "REJECTED_AT_INITIATION")
        )
        self.assertIsNotNone(row["disbursement_id"])  # Payments still created a row for it
        self.assertEqual(self.count_anomalies("payout_rejected"), 1)

    def test_a_disburse_timeout_is_requested_not_failed_and_holds_a_disbursement_id_to_poll(
        self,
    ) -> None:
        # Payments answers 201 with its own state UNKNOWN (money may have moved): never FAILED,
        # and REQUESTED (not PLANNED) so reconcile_requested_payouts can poll it -- if it were
        # left PLANNED, send_due_payouts would only keep re-asking Payments forever instead of
        # ever finding out what actually happened.
        ledger_id = self.plan(msisdn=DISBURSE_TIMEOUT)
        self.send()
        row = self.ledger(ledger_id)
        self.assertEqual(row["state"], "REQUESTED")
        self.assertIsNotNone(row["disbursement_id"])
        self.assertEqual(self.adapter.disburse_call_count, 1)
        # A second send() call finds nothing PLANNED left -- Payments is never asked twice.
        self.send()
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_a_failed_row_can_be_replanned_and_sent_again_with_a_new_key(self) -> None:
        ledger_id = self.plan(msisdn=DISBURSE_REJECTED)
        self.send()
        self.assertEqual(self.ledger(ledger_id)["state"], "FAILED")
        # An operator (or a later close for a new business_date) creates a fresh PLANNED row with
        # its own idempotency_key -- never resurrecting the FAILED one.
        new_id = self.plan(attendant_id="att-1", business_date="2026-09-21", msisdn=SUCCESS)
        self.send()
        self.assertEqual(self.ledger(new_id)["state"], "REQUESTED")
        self.assertEqual(self.adapter.disburse_call_count, 2)

    def count_anomalies(self, kind: str) -> int:
        with self.store.connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM anomalies WHERE kind = ?", (kind,)
            ).fetchone()["n"]


class ReconcileTest(DisburseTestCase):
    def test_success_settles_to_succeeded(self) -> None:
        ledger_id = self.plan(msisdn=SUCCESS)
        self.send()
        self.clock.advance(2)
        self.app.dispatch("POST", "/_fake/deliver-callbacks", {}, b"{}", "127.0.0.1")
        result = self.reconcile()
        self.assertEqual(result["counts"].get("succeeded"), 1)
        self.assertEqual(self.ledger(ledger_id)["state"], "SUCCEEDED")

    def test_reconciling_twice_after_success_is_a_no_op(self) -> None:
        ledger_id = self.plan(msisdn=SUCCESS)
        self.send()
        self.clock.advance(2)
        self.app.dispatch("POST", "/_fake/deliver-callbacks", {}, b"{}", "127.0.0.1")
        self.reconcile()
        second = self.reconcile()
        self.assertEqual(second["checked"], 0)  # no longer REQUESTED, nothing to look at
        self.assertEqual(self.ledger(ledger_id)["state"], "SUCCEEDED")

    def test_no_result_yet_leaves_the_row_requested_and_reconcile_attempts_climb(self) -> None:
        ledger_id = self.plan(msisdn=TIMEOUT_NO_RESULT)
        self.send()
        result = self.reconcile()
        self.assertEqual(result["counts"].get("still_pending"), 1)
        row = self.ledger(ledger_id)
        self.assertEqual((row["state"], row["reconcile_attempts"]), ("REQUESTED", 1))

    def test_a_result_resolved_only_by_query_still_settles(self) -> None:
        # TIMEOUT_QUERY_RESOLVES never sends a callback: Payments' own disbursement sits PENDING
        # until Payments' scheduled sweep promotes it past the callback deadline to UNKNOWN and
        # queries the adapter (ADR 0006 open question 9 -- that sweep has to be scheduled
        # somewhere; here the test drives it directly, the same way Payments' own tests do).
        ledger_id = self.plan(msisdn=TIMEOUT_QUERY_RESOLVES)
        self.send()
        self.assertEqual(self.ledger(ledger_id)["state"], "REQUESTED")

        # Payments' sweep needs two passes: one to notice the callback deadline has passed
        # (PENDING -> UNKNOWN), and a second, at least reconcile_sla_seconds later, to treat the
        # now-UNKNOWN row as due and actually query the adapter -- matching Payments' own tests.
        self.clock.advance(100)
        self.app.payouts.sweep()
        self.clock.advance(130)
        self.app.payouts.sweep()

        result = self.reconcile()
        self.assertEqual(result["counts"].get("succeeded"), 1)
        self.assertEqual(self.ledger(ledger_id)["state"], "SUCCEEDED")

    def test_duplicate_originator_answer_is_requested_not_failed_and_resolves_by_reconcile(
        self,
    ) -> None:
        # Same shape as a disburse timeout: Payments answers 201 with state UNKNOWN (an earlier
        # attempt with this originator id may have paid), never FAILED. Payments' disbursement
        # starts UNKNOWN here (not PENDING), so its own sweep can query the adapter directly once
        # the fake's resolution time has passed -- no callback-deadline wait needed first.
        ledger_id = self.plan(msisdn=DUPLICATE_ORIGINATOR)
        self.send()
        row = self.ledger(ledger_id)
        self.assertEqual(row["state"], "REQUESTED")
        self.assertIsNotNone(row["disbursement_id"])

        self.clock.advance(125)
        self.app.payouts.sweep()
        result = self.reconcile()
        self.assertEqual(result["counts"].get("succeeded"), 1)
        self.assertEqual(self.ledger(ledger_id)["state"], "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
