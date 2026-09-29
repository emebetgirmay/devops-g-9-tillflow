"""GET /_admin/invariants (ADR 0009 section 6, work item G3-3).

The counts k6's soak run is meant to assert stay at zero throughout a whole
run — these just prove the endpoint reports them correctly for a real
credited payment, not that k6 itself is wired (that's G3-4, separate work).
"""

from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401
from helpers import ServiceTestCase, key


class InvariantsTest(ServiceTestCase):
    def test_zero_on_a_fresh_database(self) -> None:
        body = self.call("GET", "/_admin/invariants").body
        self.assertTrue(body["credits_equal_succeeded_payments"])
        self.assertEqual(body["succeeded_payments"], 0)
        self.assertEqual(body["payment_credits"], 0)
        self.assertEqual(body["duplicate_ledger_entries"], 0)
        self.assertEqual(body["payout_keys_with_multiple_live_disbursements"], 0)
        self.assertEqual(body["payments_declined_by_a_timeout"], 0)

    def test_credits_track_a_real_succeeded_payment(self) -> None:
        self.pay(idem=key(1))
        self.advance(2)
        self.deliver()
        body = self.call("GET", "/_admin/invariants").body
        self.assertTrue(body["credits_equal_succeeded_payments"])
        self.assertEqual(body["succeeded_payments"], 1)
        self.assertEqual(body["payment_credits"], 1)

    def test_replayed_callback_never_produces_a_duplicate_credit(self) -> None:
        self.pay(idem=key(1))
        self.advance(2)
        self.deliver()  # first delivery: applies and credits
        self.deliver()  # nothing new due; a second call is a no-op either way
        body = self.call("GET", "/_admin/invariants").body
        self.assertEqual(body["succeeded_payments"], 1)
        self.assertEqual(body["payment_credits"], 1)
        self.assertEqual(body["duplicate_ledger_entries"], 0)


if __name__ == "__main__":
    unittest.main()
