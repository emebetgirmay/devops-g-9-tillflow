"""The fakes are shared by every request thread of the Payments server (ThreadingHTTPServer).

Regression for the G3 k6 soak (evidence/reliability-ops/k6-analysis.md): /_fake/deliver-callbacks
iterated the charges dict while other request threads were adding to it, raising
"dictionary changed size during iteration"; the handler thread died without a response and the
ALB answered 502 (110 times in 35,276 requests). Each test runs writers and a reader together
and fails if any thread raised or a delivery went missing or was handed out twice.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest

from mpesa import ChargeRequest, DisbursementRequest, FakeAdapter, ManualClock

WRITERS = 4
PER_WRITER = 400


class ConcurrentAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        # Switch threads every microsecond instead of every 5 ms, so a reader's scan is almost
        # certainly interrupted by a writer's insert. Without the lock this reproduces the soak's
        # RuntimeError on every run; with it the scan and the insert simply take turns.
        self._interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)

    def tearDown(self) -> None:
        sys.setswitchinterval(self._interval)

    def run_threads(self, writer, reader) -> list[BaseException]:
        errors: list[BaseException] = []
        stop = threading.Event()

        def guard(fn):
            def run():
                try:
                    fn(stop)
                except BaseException as exc:  # noqa: BLE001 - the test reports whatever escaped
                    errors.append(exc)
                    stop.set()

            return run

        writers = [threading.Thread(target=guard(lambda s, w=w: writer(w, s))) for w in range(WRITERS)]
        readers = [threading.Thread(target=guard(reader)) for _ in range(2)]
        for t in readers + writers:
            t.start()
        for t in writers:
            t.join()
        stop.set()
        for t in readers:
            t.join()
        return errors

    def test_charges_while_callbacks_are_collected(self) -> None:
        clock = ManualClock()
        adapter = FakeAdapter(clock=clock)
        collected: list = []

        def writer(w: int, _stop) -> None:
            for i in range(PER_WRITER):
                adapter.initiate_charge(
                    ChargeRequest(
                        idempotency_key=f"conc-charge-{w:02d}-{i:06d}",
                        tenant_id="tenant-a",
                        msisdn="254000000001",  # FakeAdapter: SUCCESS
                        amount_minor=100,
                    )
                )
                clock.advance(1)

        def reader(stop) -> None:
            while not stop.is_set():
                collected.extend(adapter.due_callbacks())
                # Yield between calls: Python locks are not fair, and a reader spinning on a
                # growing scan would starve the writers (production calls this about once a second).
                time.sleep(0.0005)

        self.assertEqual(self.run_threads(writer, reader), [])
        clock.advance(10_000)
        collected.extend(adapter.due_callbacks())
        refs = [d.provider_ref for d in collected]
        self.assertEqual(len(set(refs)), WRITERS * PER_WRITER, "a charge's callback went missing")
        self.assertEqual(len(refs), len(set(refs)), "a callback was handed out twice")

    def test_payouts_while_results_are_collected(self) -> None:
        clock = ManualClock()
        adapter = FakeAdapter(clock=clock)
        collected: list = []

        def writer(w: int, _stop) -> None:
            for i in range(PER_WRITER):
                adapter.disburse(
                    DisbursementRequest(
                        idempotency_key=f"conc-payout-{w:02d}-{i:06d}",
                        tenant_id="tenant-a",
                        originator_conversation_id=f"c{w:02d}x{i:06d}",
                        msisdn="254000000101",  # FakeAdapter: SUCCESS
                        amount_minor=5_000,
                    )
                )
                clock.advance(1)

        def reader(stop) -> None:
            while not stop.is_set():
                collected.extend(adapter.due_disbursement_results())
                # Yield between calls: Python locks are not fair, and a reader spinning on a
                # growing scan would starve the writers (production calls this about once a second).
                time.sleep(0.0005)

        self.assertEqual(self.run_threads(writer, reader), [])
        clock.advance(10_000)
        collected.extend(adapter.due_disbursement_results())
        refs = [d.provider_ref for d in collected]
        self.assertEqual(len(set(refs)), WRITERS * PER_WRITER, "a payout's result went missing")
        self.assertEqual(len(refs), len(set(refs)), "a result was handed out twice")


if __name__ == "__main__":
    unittest.main()
