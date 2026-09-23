"""Pure commission math: no I/O, deterministic, integer-only."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

import _bootstrap  # noqa: F401

from ledger.calc import (
    business_date,
    business_day_bounds,
    fold_carry_forward,
    sale_commission_minor,
)


class BusinessDateTest(unittest.TestCase):
    def test_eat_offset_can_move_the_calendar_date(self) -> None:
        # 21:30 UTC on the 20th is 00:30 EAT on the 21st.
        paid_at = datetime(2026, 9, 20, 21, 30, tzinfo=timezone.utc)
        self.assertEqual(business_date(paid_at, utc_offset_hours=3), "2026-09-21")

    def test_naive_datetime_is_treated_as_utc(self) -> None:
        paid_at = datetime(2026, 9, 20, 12, 0)  # noqa: DTZ001 (deliberately naive, that is the case under test)
        self.assertEqual(business_date(paid_at, utc_offset_hours=3), "2026-09-20")

    def test_bounds_round_trip(self) -> None:
        since, until = business_day_bounds(date(2026, 9, 21), utc_offset_hours=3)
        # A sale one minute inside each edge falls in this business day; one minute outside does not.
        self.assertEqual(business_date(since, 3), "2026-09-21")
        self.assertEqual(business_date(until, 3), "2026-09-22")
        from datetime import timedelta

        self.assertEqual(business_date(until - timedelta(minutes=1), 3), "2026-09-21")
        self.assertEqual(business_date(since - timedelta(minutes=1), 3), "2026-09-20")

    def test_zero_offset_is_plain_utc_calendar_day(self) -> None:
        since, until = business_day_bounds(date(2026, 1, 1), utc_offset_hours=0)
        self.assertEqual(since, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(until, datetime(2026, 1, 2, tzinfo=timezone.utc))


class SaleCommissionTest(unittest.TestCase):
    def test_basis_points(self) -> None:
        self.assertEqual(sale_commission_minor(100_000, 500), 5_000)  # 5% of 1000 KES
        self.assertEqual(sale_commission_minor(0, 500), 0)
        self.assertEqual(sale_commission_minor(100_000, 0), 0)

    def test_floors_never_rounds_up(self) -> None:
        # 333 bps of 101 minor units is 3.3633 -> floors to 3, never 4.
        self.assertEqual(sale_commission_minor(101, 333), 3)

    def test_rejects_negative_inputs(self) -> None:
        with self.assertRaises(ValueError):
            sale_commission_minor(-1, 100)
        with self.assertRaises(ValueError):
            sale_commission_minor(100, -1)


class FoldCarryForwardTest(unittest.TestCase):
    def test_exact_whole_shillings_pays_everything(self) -> None:
        plan = fold_carry_forward(50_000, 0, minimum_minor=1_000)
        self.assertEqual((plan.payable_minor, plan.new_carry_forward_minor), (50_000, 0))

    def test_sub_shilling_remainder_is_carried_never_dropped(self) -> None:
        plan = fold_carry_forward(50_049, 0, minimum_minor=1_000)
        self.assertEqual((plan.payable_minor, plan.new_carry_forward_minor), (50_000, 49))

    def test_prior_carry_forward_is_added_in(self) -> None:
        plan = fold_carry_forward(50_000, 149, minimum_minor=1_000)
        self.assertEqual((plan.payable_minor, plan.new_carry_forward_minor), (50_100, 49))

    def test_below_minimum_pays_nothing_and_carries_everything(self) -> None:
        plan = fold_carry_forward(500, 0, minimum_minor=1_000)
        self.assertEqual((plan.payable_minor, plan.new_carry_forward_minor), (0, 500))

    def test_at_exactly_the_minimum_is_payable(self) -> None:
        plan = fold_carry_forward(1_000, 0, minimum_minor=1_000)
        self.assertEqual((plan.payable_minor, plan.new_carry_forward_minor), (1_000, 0))

    def test_conservation_holds_across_random_looking_inputs(self) -> None:
        for today, carry in ((0, 0), (999, 999), (123_456, 789), (1, 998), (250_000, 25_050)):
            with self.subTest(today=today, carry=carry):
                plan = fold_carry_forward(today, carry, minimum_minor=1_000)
                self.assertEqual(plan.payable_minor + plan.new_carry_forward_minor, today + carry)
                self.assertEqual(plan.payable_minor % 100, 0)

    def test_rejects_negative_inputs(self) -> None:
        with self.assertRaises(ValueError):
            fold_carry_forward(-1, 0, minimum_minor=1_000)
        with self.assertRaises(ValueError):
            fold_carry_forward(0, -1, minimum_minor=1_000)


if __name__ == "__main__":
    unittest.main()
