"""Daily close: turn confirmed-paid POS sales into payout_ledger rows (ADR 0008 sections 1-2).

For one tenant and one business day:

1. Pull every PAID sale POS reports for that day (``ledger/pos_client.py``), and record one
   ``payout_items`` row per sale with its computed commission. ``sale_id`` is unique, so replaying
   a close (the same day, twice, or concurrently) never counts a sale's commission a second time
   -- a repeat insert is a silent no-op, not an error.
2. For each attendant touched, fold today's commission total together with whatever carried
   forward from before (``ledger/calc.py::fold_carry_forward``) into a single ``payout_ledger`` row,
   only if a live (non-``FAILED``) one does not already exist for that
   ``(tenant, attendant, business_date)`` -- the unique index on that triple is the same guarantee
   ADR 0008 asks for, so a second close for an already-closed day changes nothing.
3. Every currently unlinked ``payout_items`` row for that attendant is linked to the new ledger
   row, not only today's -- that is what lets an amount carried forward from an earlier day
   finally get paid once it crosses the minimum.

This module never calls Payments; ``ledger/disburse.py`` reads what this leaves in ``PLANNED``.
"""

from __future__ import annotations

import sys
import time
import uuid
from datetime import date

from ledger.calc import business_day_bounds, fold_carry_forward, sale_commission_minor
from ledger.common import idempotency_key_for
from ledger.config import Settings
from ledger.pos_client import POSClient, POSError
from ledger.store import Store


def close_business_day(
    store: Store, pos_client: POSClient, settings: Settings, tenant_id: str, business_day: date
) -> dict:
    """Close one tenant's one business day. Safe to call again for the same day: nothing already
    recorded (an item, or a non-FAILED ledger row) is recreated or double counted."""
    since_dt, until_dt = business_day_bounds(business_day, settings.business_day_utc_offset_hours)
    business_date_str = business_day.isoformat()

    sales_seen = 0
    touched: dict[str, str] = {}  # attendant_id -> msisdn (from the most recent sale seen)
    try:
        for sale in pos_client.paid_sales(
            tenant_id, since_dt.isoformat(), until_dt.isoformat(), limit=settings.close_page_limit
        ):
            sales_seen += 1
            commission_minor = sale_commission_minor(sale.total_minor, sale.commission_rate_bps)
            now = time.time()
            with store.tx() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO payout_items (id, sale_id, tenant_id, attendant_id,"
                    " business_date, sale_total_minor, commission_rate_bps, commission_minor,"
                    " paid_at, payout_ledger_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        "itm_" + uuid.uuid4().hex,
                        sale.sale_id,
                        tenant_id,
                        sale.attendant_id,
                        business_date_str,
                        sale.total_minor,
                        sale.commission_rate_bps,
                        commission_minor,
                        sale.paid_at,
                        now,
                    ),
                )
            touched[sale.attendant_id] = sale.attendant_msisdn
    except POSError as exc:
        print(f"commission close: {tenant_id} {business_date_str}: {exc}", file=sys.stderr)
        return {
            "tenant_id": tenant_id,
            "business_date": business_date_str,
            "sales_seen": sales_seen,
            "attendants_closed": 0,
            "aborted": str(exc),
        }

    attendants_closed = 0
    for attendant_id, msisdn in touched.items():
        if _close_attendant(store, settings, tenant_id, attendant_id, business_date_str, msisdn):
            attendants_closed += 1

    with store.tx() as conn:
        conn.execute(
            "INSERT INTO close_runs (tenant_id, business_date, sales_seen, attendants_closed,"
            " ran_at) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, business_date_str, sales_seen, attendants_closed, time.time()),
        )

    return {
        "tenant_id": tenant_id,
        "business_date": business_date_str,
        "sales_seen": sales_seen,
        "attendants_closed": attendants_closed,
        "aborted": None,
    }


def _close_attendant(
    store: Store,
    settings: Settings,
    tenant_id: str,
    attendant_id: str,
    business_date: str,
    msisdn: str,
) -> bool:
    now = time.time()
    with store.tx() as conn:
        existing = conn.execute(
            "SELECT 1 FROM payout_ledger WHERE tenant_id = ? AND attendant_id = ?"
            " AND business_date = ? AND state <> 'FAILED'",
            (tenant_id, attendant_id, business_date),
        ).fetchone()
        if existing is not None:
            return False  # already closed (or later re-closed after a FAILED retry) for this day

        today_total = conn.execute(
            "SELECT COALESCE(SUM(commission_minor), 0) AS total FROM payout_items"
            " WHERE tenant_id = ? AND attendant_id = ? AND business_date = ?",
            (tenant_id, attendant_id, business_date),
        ).fetchone()["total"]
        carry_row = conn.execute(
            "SELECT amount_minor FROM carry_forward WHERE tenant_id = ? AND attendant_id = ?",
            (tenant_id, attendant_id),
        ).fetchone()
        carry_before = carry_row["amount_minor"] if carry_row else 0

        plan = fold_carry_forward(today_total, carry_before, settings.payout_min_minor)

        conn.execute(
            "INSERT INTO carry_forward (tenant_id, attendant_id, amount_minor, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (tenant_id, attendant_id) DO UPDATE SET"
            " amount_minor = excluded.amount_minor, updated_at = excluded.updated_at",
            (tenant_id, attendant_id, plan.new_carry_forward_minor, now),
        )

        if plan.payable_minor <= 0:
            return False

        ledger_id = "pol_" + uuid.uuid4().hex
        idem_key = idempotency_key_for(tenant_id, attendant_id, business_date)
        conn.execute(
            "INSERT INTO payout_ledger (id, tenant_id, attendant_id, business_date, currency,"
            " amount_minor, msisdn, state, idempotency_key, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'KES', ?, ?, 'PLANNED', ?, ?, ?)",
            (
                ledger_id,
                tenant_id,
                attendant_id,
                business_date,
                plan.payable_minor,
                msisdn,
                idem_key,
                now,
                now,
            ),
        )
        # Sweep in every unlinked item for this attendant, not only today's — that is how a
        # carried-forward amount from an earlier day finally reaches a ledger row.
        conn.execute(
            "UPDATE payout_items SET payout_ledger_id = ? WHERE tenant_id = ? AND attendant_id = ?"
            " AND payout_ledger_id IS NULL",
            (ledger_id, tenant_id, attendant_id),
        )
        return True
