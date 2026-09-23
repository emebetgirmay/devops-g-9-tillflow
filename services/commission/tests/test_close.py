"""core/close.py against a real HTTP FakePOSServer: idempotent, replay-safe, carries forward."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import _bootstrap  # noqa: F401
from fake_pos import FakePOSSale, FakePOSServer

from ledger.close import close_business_day
from ledger.config import Settings
from ledger.pos_client import POSClient
from ledger.store import Store

TENANT = "tenant-a"
DAY = date(2026, 9, 21)
KES = "KES"


def _sale(
    sale_id: str,
    attendant_id: str,
    hour: int,
    day: date = DAY,
    total_minor: int = 100_000,
    rate_bps: int = 500,
    msisdn: str = "254000000101",
) -> FakePOSSale:
    # An hour comfortably inside 00:00-21:00 UTC keeps the sale inside `day`'s EAT business
    # window (EAT = UTC+3), regardless of which calendar day is being tested.
    paid_at = datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc).isoformat()
    return FakePOSSale(sale_id, attendant_id, msisdn, rate_bps, total_minor, KES, paid_at)


class CloseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.pos = FakePOSServer()
        self.addCleanup(self.pos.close)
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.settings = replace(
            Settings(),
            db_path=str(Path(self._dir.name) / "commission.db"),
            pos_base_url=self.pos.base_url,
        )
        self.store = Store(self.settings.db_path)
        self.pos_client = POSClient(self.pos.base_url)

    def close(self, business_day: date = DAY) -> dict:
        return close_business_day(self.store, self.pos_client, self.settings, TENANT, business_day)

    def items(self) -> list:
        with self.store.connection() as conn:
            return conn.execute("SELECT * FROM payout_items ORDER BY sale_id").fetchall()

    def ledger_rows(self) -> list:
        with self.store.connection() as conn:
            return conn.execute("SELECT * FROM payout_ledger ORDER BY business_date").fetchall()

    def carry(self, attendant_id: str = "att-1") -> int:
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT amount_minor FROM carry_forward WHERE tenant_id = ? AND attendant_id = ?",
                (TENANT, attendant_id),
            ).fetchone()
        return row["amount_minor"] if row else 0


class BasicCloseTest(CloseTestCase):
    def test_one_sale_above_minimum_closes_to_a_ledger_row(self) -> None:
        self.pos.add(
            TENANT, _sale("s1", "att-1", 9, total_minor=100_000, rate_bps=500)
        )  # 5% of 1000 KES = 50 KES
        result = self.close()
        self.assertEqual((result["sales_seen"], result["attendants_closed"]), (1, 1))

        (ledger,) = self.ledger_rows()
        self.assertEqual(
            (ledger["amount_minor"], ledger["state"], ledger["msisdn"]),
            (5_000, "PLANNED", "254000000101"),
        )
        (item,) = self.items()
        self.assertEqual(
            (item["sale_id"], item["commission_minor"], item["payout_ledger_id"]),
            ("s1", 5_000, ledger["id"]),
        )
        self.assertEqual(self.carry(), 0)

    def test_below_minimum_carries_forward_with_no_ledger_row(self) -> None:
        self.pos.add(
            TENANT, _sale("s1", "att-1", 9, total_minor=1_000, rate_bps=500)
        )  # 5% of 10 KES = 0.50 KES
        result = self.close()
        self.assertEqual(result["attendants_closed"], 0)
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.carry(), 50)
        (item,) = self.items()
        self.assertIsNone(item["payout_ledger_id"])  # recorded, just not yet paid

    def test_multiple_attendants_get_separate_ledger_rows(self) -> None:
        self.pos.add(TENANT, _sale("s1", "att-1", 9, total_minor=200_000, rate_bps=500))
        self.pos.add(
            TENANT,
            _sale("s2", "att-2", 10, total_minor=400_000, rate_bps=250, msisdn="254000000102"),
        )
        self.close()
        rows = {r["attendant_id"]: r["amount_minor"] for r in self.ledger_rows()}
        self.assertEqual(rows, {"att-1": 10_000, "att-2": 10_000})

    def test_sales_outside_the_window_are_not_included(self) -> None:
        # 20:59 UTC on the 20th is 23:59 EAT on the 20th -- the day before.
        early = FakePOSSale(
            "s0",
            "att-1",
            "254000000101",
            500,
            100_000,
            KES,
            datetime(2026, 9, 20, 20, 59, tzinfo=timezone.utc).isoformat(),
        )
        self.pos.add(TENANT, early)
        self.pos.add(TENANT, _sale("s1", "att-1", 9))
        self.close()
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.items()[0]["sale_id"], "s1")


class IdempotencyTest(CloseTestCase):
    def test_closing_the_same_day_twice_does_not_double_count(self) -> None:
        self.pos.add(TENANT, _sale("s1", "att-1", 9, total_minor=100_000, rate_bps=500))
        first = self.close()
        second = self.close()
        self.assertEqual((first["attendants_closed"], second["attendants_closed"]), (1, 0))
        self.assertEqual(len(self.ledger_rows()), 1)
        self.assertEqual(self.ledger_rows()[0]["amount_minor"], 5_000)

    def test_a_late_arriving_sale_after_close_is_carried_not_lost_or_double_paid(self) -> None:
        self.pos.add(TENANT, _sale("s1", "att-1", 9, total_minor=100_000, rate_bps=500))
        self.close()
        self.pos.add(TENANT, _sale("s2", "att-1", 10, total_minor=100_000, rate_bps=500))
        result = self.close()  # closing the SAME day again: s1's ledger already exists
        self.assertEqual(
            result["attendants_closed"], 0
        )  # existing non-FAILED ledger blocks a new one
        self.assertEqual(len(self.ledger_rows()), 1)
        s2 = next(i for i in self.items() if i["sale_id"] == "s2")
        self.assertIsNone(
            s2["payout_ledger_id"]
        )  # not lost -- sitting unlinked, ready for the next close

    def test_concurrent_close_of_the_same_day_still_produces_one_ledger_row(self) -> None:
        import threading

        self.pos.add(TENANT, _sale("s1", "att-1", 9, total_minor=100_000, rate_bps=500))
        errors: list[Exception] = []

        def go() -> None:
            try:
                self.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.ledger_rows()), 1)
        self.assertEqual(self.ledger_rows()[0]["amount_minor"], 5_000)


class CarryForwardTest(CloseTestCase):
    def test_carry_forward_accumulates_across_days_until_it_crosses_the_minimum(self) -> None:
        # Each day's commission alone is below the KSh 10 minimum; across three days it crosses it.
        day1, day2, day3 = date(2026, 9, 19), date(2026, 9, 20), date(2026, 9, 21)
        self.pos.add(
            TENANT, _sale("s1", "att-1", 9, day=day1, total_minor=6_000, rate_bps=500)
        )  # floor(300)=KSh3
        self.close(day1)
        self.assertEqual(self.carry(), 300)
        self.assertEqual(self.ledger_rows(), [])

        self.pos.add(TENANT, _sale("s2", "att-1", 9, day=day2, total_minor=6_000, rate_bps=500))
        self.close(day2)
        self.assertEqual(self.carry(), 600)
        self.assertEqual(self.ledger_rows(), [])

        self.pos.add(
            TENANT, _sale("s3", "att-1", 9, day=day3, total_minor=100_000, rate_bps=500)
        )  # +5000
        result = self.close(day3)
        self.assertEqual(result["attendants_closed"], 1)
        (ledger,) = self.ledger_rows()
        self.assertEqual(ledger["business_date"], day3.isoformat())
        self.assertEqual(
            ledger["amount_minor"], 5_600
        )  # 300 + 300 + 5000, floored to whole shillings
        self.assertEqual(self.carry(), 0)

        # All three sales, including the two from earlier carried days, end up linked to the
        # one ledger row that finally paid them.
        linked = {i["sale_id"]: i["payout_ledger_id"] for i in self.items()}
        self.assertEqual(set(linked.values()), {ledger["id"]})

    def test_conservation_total_commission_equals_paid_plus_still_carried(self) -> None:
        for day_offset, (total, rate) in enumerate([(6_000, 500), (100_000, 333), (250_000, 125)]):
            day = DAY + timedelta(days=day_offset)
            self.pos.add(
                TENANT,
                _sale(f"s{day_offset}", "att-1", 9, day=day, total_minor=total, rate_bps=rate),
            )
            self.close(day)

        from ledger.calc import sale_commission_minor

        expected_total = sum(
            sale_commission_minor(t, r) for t, r in [(6_000, 500), (100_000, 333), (250_000, 125)]
        )
        paid = sum(r["amount_minor"] for r in self.ledger_rows())
        self.assertEqual(paid + self.carry(), expected_total)


class ErrorHandlingTest(CloseTestCase):
    def test_unreachable_pos_aborts_that_tenant_cleanly(self) -> None:
        self.pos.close()  # server is now down
        result = self.close()
        self.assertIsNotNone(result["aborted"])
        self.assertEqual(result["sales_seen"], 0)
        self.assertEqual(self.ledger_rows(), [])


if __name__ == "__main__":
    unittest.main()
