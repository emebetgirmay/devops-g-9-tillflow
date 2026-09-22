"""Pure commission math: business-day bucketing, per-sale commission, and whole-shilling rounding.

No I/O, no randomness, integer arithmetic only (no floats on the money path). These are the
judgment calls ADR 0008 open question 3 and 4 leave to Product; until Product decides otherwise,
this module picks the conservative default and documents it so it is easy to find and change:

- Business day: a plain EAT (UTC+3) calendar day, no special cutoff hour.
- Rounding: floor, at both the per-sale step and the whole-shilling step, so a rounding error can
  only ever underpay an attendant relative to their exact bps share, never overpay. The floored
  remainder is never dropped — see ``fold_carry_forward``.
- Below the KSh 10 minimum, or when the whole-shilling floor leaves nothing at or above it, the
  commission accrues rather than attempting (and failing) a disbursement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

SHILLING_MINOR = 100


def business_date(paid_at: datetime, utc_offset_hours: int) -> str:
    """The local calendar date (YYYY-MM-DD) `paid_at` falls on, for the configured UTC offset."""
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=timezone.utc)
    local = paid_at.astimezone(timezone.utc) + timedelta(hours=utc_offset_hours)
    return local.date().isoformat()


def business_day_bounds(business_day: date, utc_offset_hours: int) -> tuple[datetime, datetime]:
    """The [since, until) UTC window a local calendar date covers, for the configured offset."""
    offset = timedelta(hours=utc_offset_hours)
    start_local = datetime.combine(business_day, datetime.min.time(), tzinfo=timezone.utc)
    since = start_local - offset
    until = since + timedelta(days=1)
    return since, until


def sale_commission_minor(total_minor: int, commission_rate_bps: int) -> int:
    """Floor division only: commission_rate_bps is basis points (1/100 of a percent)."""
    if total_minor < 0 or commission_rate_bps < 0:
        raise ValueError("total_minor and commission_rate_bps must not be negative")
    return (total_minor * commission_rate_bps) // 10_000


@dataclass(frozen=True)
class ClosePlan:
    """What one attendant's close for one business day resolves to."""

    payable_minor: int  # 0 if nothing crosses the minimum this time; still a multiple of 100
    new_carry_forward_minor: int  # exact remainder (sub-shilling and/or sub-minimum), never lost


def fold_carry_forward(
    today_total_minor: int, prior_carry_forward_minor: int, minimum_minor: int
) -> ClosePlan:
    """Combine today's exact commission total with whatever was carried forward, floor to whole
    shillings, and hold back anything below the payout minimum. Exact: payable + new carry
    forward always equals today_total_minor + prior_carry_forward_minor."""
    if today_total_minor < 0 or prior_carry_forward_minor < 0:
        raise ValueError("amounts must not be negative")
    available = today_total_minor + prior_carry_forward_minor
    payable = (available // SHILLING_MINOR) * SHILLING_MINOR
    if payable < minimum_minor:
        return ClosePlan(payable_minor=0, new_carry_forward_minor=available)
    return ClosePlan(payable_minor=payable, new_carry_forward_minor=available - payable)
