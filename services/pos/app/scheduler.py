"""Background sweep that reconciles sales stuck in PAYMENT_REQUESTED.

Closes the gap left by app/routers/sales.py::reconcile_payment being purely
on-demand: without this, a sale only ever finds out it settled if something
explicitly calls that endpoint. Off by default — set POS_RECONCILE_SCHEDULER
to enable it (see services/pos/README.md for the full env var list).

Deliberately self-contained: no Platform/EventBridge wiring needed, no
change to the product contract, and disabled by default so it never runs
under pytest (which never sets these env vars) or in a fresh dev checkout
without someone opting in.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, sessionmaker

from . import models
from .db import SessionLocal
from .payment_outcomes import ReconcileAmountMismatch, reconcile_sale
from .payments_client import PaymentsClient, get_payments_client
from .state import SaleStatus

logger = logging.getLogger("pos.scheduler")


def is_enabled() -> bool:
    return os.environ.get("POS_RECONCILE_SCHEDULER", "").strip().lower() in ("1", "true", "yes", "on")


def interval_seconds() -> float:
    return float(os.environ.get("POS_RECONCILE_INTERVAL_SECONDS", "30"))


def stale_after_seconds() -> float:
    return float(os.environ.get("POS_RECONCILE_STALE_AFTER_SECONDS", "15"))


def run_once(
    session_factory: sessionmaker | None = None,
    get_client: Callable[[], PaymentsClient] | None = None,
) -> int:
    """Reconcile every sale stuck in PAYMENT_REQUESTED past the stale
    threshold. Returns how many sales were attempted (not how many
    actually transitioned — a sale still pending at Payments is an
    attempt, not a failure). One sale erroring never stops the rest of
    the sweep.
    """
    session_factory = session_factory or SessionLocal
    payments_client = (get_client or get_payments_client)()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds())

    db: Session = session_factory()
    attempted = 0
    try:
        stale_sales = (
            db.query(models.Sale)
            .filter(models.Sale.status == SaleStatus.PAYMENT_REQUESTED.value)
            .filter(models.Sale.updated_at <= cutoff)
            .all()
        )
        for sale in stale_sales:
            attempted += 1
            try:
                reconcile_sale(db, sale, payments_client)
            except ReconcileAmountMismatch:
                logger.error("reconcile amount mismatch for sale %s", sale.id)
            except Exception:  # noqa: BLE001 — one bad sale must not stop the sweep
                logger.exception("reconcile sweep failed for sale %s", sale.id)
    finally:
        db.close()
    return attempted


async def reconcile_loop() -> None:
    interval = interval_seconds()
    while True:
        try:
            attempted = await asyncio.to_thread(run_once)
            if attempted:
                logger.info("reconcile sweep attempted %d sale(s)", attempted)
        except Exception:  # noqa: BLE001 — the loop itself must survive a bad sweep
            logger.exception("reconcile sweep raised")
        await asyncio.sleep(interval)
