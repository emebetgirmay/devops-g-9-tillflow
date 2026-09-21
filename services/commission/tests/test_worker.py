"""Commission worker: one payout per (tenant, attendant, period), however often it runs."""

from __future__ import annotations

import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401
from helpers import PaymentsServerCase

import worker
from worker import Entry, idempotency_key_for, read_entries, run

SUCCESS = "254000000101"
INSUFFICIENT = "254000000102"


def entry(
    attendant: str = "att-1",
    period: str = "2026-09-20",
    msisdn: str = SUCCESS,
    amount: int = 500_000,
):
    return Entry("tenant-a", attendant, period, msisdn, amount)


class KeyTest(unittest.TestCase):
    def test_key_is_deterministic_and_fits_the_payments_key_format(self) -> None:
        key = idempotency_key_for("t", "a", "p")
        self.assertEqual(key, idempotency_key_for("t", "a", "p"))
        self.assertRegex(key, r"^[A-Za-z0-9_-]{16,64}$")
        self.assertNotEqual(key, idempotency_key_for("t", "a", "q"))
        self.assertNotEqual(key, idempotency_key_for("t", "b", "p"))


class WorkerAgainstPaymentsTest(PaymentsServerCase):
    def test_running_twice_yields_exactly_one_payout_per_entry(self) -> None:
        entries = [entry("att-1"), entry("att-2"), entry("att-3"), entry("att-1")]  # one repeated
        first = run(entries, self.base_url)
        second = run(entries, self.base_url)
        self.assertEqual(first["counts"], {"created": 3, "duplicate_in_input": 1})
        self.assertEqual(second["counts"], {"already_requested": 3, "duplicate_in_input": 1})
        self.assertEqual(self.count("disbursements"), 3)
        self.assertEqual(self.adapter.disburse_call_count, 3)
        self.deliver_results()
        self.assertEqual(self.count("ledger_entries", "entry_type = 'DISBURSEMENT_DEBIT'"), 3)
        self.assertEqual(run(entries, self.base_url)["counts"]["already_requested"], 3)
        self.assertEqual(self.adapter.disburse_call_count, 3)

    def test_two_workers_at_once_still_pay_once(self) -> None:
        entries = [entry("att-1"), entry("att-2")]
        summaries: list[dict] = []
        lock = threading.Lock()

        def go() -> None:
            summary = run(entries, self.base_url)
            with lock:
                summaries.append(summary)

        threads = [threading.Thread(target=go) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.adapter.disburse_call_count, 2)
        self.assertEqual(self.count("disbursements"), 2)
        created = sum(s["counts"].get("created", 0) for s in summaries)
        self.assertEqual(created, 2)

    def test_amounts_the_provider_cannot_take_are_held_and_the_rest_proceed(self) -> None:
        entries = [entry("att-1", amount=500), entry("att-2", amount=25_000_100), entry("att-3")]
        summary = run(entries, self.base_url)
        self.assertEqual(
            summary["counts"],
            {"held_amount_below_minimum": 1, "held_amount_above_ceiling": 1, "created": 1},
        )
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_a_paused_payouts_switch_stops_the_run(self) -> None:
        run([entry("att-1", msisdn=INSUFFICIENT)], self.base_url)
        self.deliver_results()
        summary = run([entry("att-2"), entry("att-3")], self.base_url)
        self.assertEqual(summary["aborted"], "payouts_disabled")
        self.assertEqual(summary["counts"], {"payouts_disabled": 1})
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_an_unreachable_payments_service_aborts_without_sending(self) -> None:
        summary = run([entry("att-1"), entry("att-2")], "http://127.0.0.1:9")
        self.assertTrue(summary["aborted"].startswith("payments_unreachable"))
        self.assertEqual(summary["counts"], {"not_sent_unreachable": 1})

    def test_changed_details_for_the_same_payout_are_a_conflict_not_a_second_payment(self) -> None:
        run([entry("att-1", amount=500_000)], self.base_url)
        summary = run([entry("att-1", amount=900_000)], self.base_url)
        self.assertEqual(summary["counts"], {"conflict_needs_review": 1})
        self.assertEqual(worker.exit_code(summary), worker.EXIT_PROBLEMS)
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_the_command_line_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "due.csv"
            path.write_text(
                "tenant_id,attendant_id,payout_period,msisdn,amount\n"
                f"tenant-a,att-1,2026-09-20,{SUCCESS},500000\n"
                f"tenant-a,att-2,2026-09-20,{SUCCESS},500000\n"
            )
            env = {"COMMISSION_INPUT": str(path), "PAYMENTS_URL": self.base_url}
            with mock.patch.dict(os.environ, env), mock.patch("builtins.print"):
                self.assertEqual(worker.main(), worker.EXIT_OK)
                self.assertEqual(worker.main(), worker.EXIT_OK)
        self.assertEqual(self.adapter.disburse_call_count, 2)


class InputTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "in.csv"
        path.write_text(text)
        return path

    def test_reads_entries(self) -> None:
        path = self.write(
            "tenant_id,attendant_id,payout_period,msisdn,amount\nt,a,p,254000000101,1000\n"
        )
        self.assertEqual(read_entries(path), [Entry("t", "a", "p", "254000000101", 1000)])

    def test_rejects_bad_input(self) -> None:
        cases = {
            "missing column": "tenant_id,attendant_id\nt,a\n",
            "non-integer amount": "tenant_id,attendant_id,payout_period,msisdn,amount\nt,a,p,254000000101,10.5\n",
            "blank field": "tenant_id,attendant_id,payout_period,msisdn,amount\nt,,p,254000000101,1000\n",
        }
        for name, text in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_entries(self.write(text))

    def test_main_reports_bad_input_and_a_missing_setting(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("sys.stderr"):
            self.assertEqual(worker.main(), worker.EXIT_BAD_INPUT)
        with (
            mock.patch.dict(os.environ, {"COMMISSION_INPUT": "/no/such/file.csv"}),
            mock.patch("sys.stderr"),
        ):
            self.assertEqual(worker.main(), worker.EXIT_BAD_INPUT)

    def test_classification_of_payments_answers(self) -> None:
        cases = [
            (201, {}, "created"),
            (200, {}, "already_requested"),
            (409, {"error": "payout_already_requested"}, "already_requested"),
            (409, {"error": "idempotency_in_flight"}, "in_flight_retry_next_run"),
            (409, {"error": "idempotency_key_payload_mismatch"}, "conflict_needs_review"),
            (422, {"error": "amount_below_minimum"}, "held_amount_below_minimum"),
            (503, {"error": "payouts_disabled"}, "payouts_disabled"),
            (500, {}, "rejected_500"),
        ]
        for status, reply, expected in cases:
            with self.subTest(status=status, reply=reply):
                self.assertEqual(worker.classify(status, reply), expected)
        self.assertTrue(re.match(r"^[a-z_0-9]+$", worker.classify(400, {})))


if __name__ == "__main__":
    unittest.main()
