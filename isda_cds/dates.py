"""Date utilities for the ISDA CDS Standard Model.

Conventions implemented:
- ACT/360 day count for fee accrual and money-market deposits
- ACT/365F for converting dates to year fractions on the time axis
- Quarterly IMM-style CDS dates (20 Mar / 20 Jun / 20 Sep / 20 Dec)
- Following business-day adjustment (weekends only; the open-source ISDA
  model defaults to a weekend-only calendar unless holiday files are given)

Dates are plain ``datetime.date`` objects (whole days, no timezones).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

__all__ = [
    "ymd",
    "add_days",
    "add_months",
    "is_weekend",
    "adjust_following",
    "days_between",
    "act360",
    "act365",
    "next_cds_date",
    "prev_cds_date",
    "is_cds_date",
    "standard_cds_maturity",
    "FeePeriod",
    "build_fee_schedule",
    "parse_iso",
    "to_iso",
]


def ymd(y: int, m: int, d: int) -> date:
    """Construct a date."""
    return date(y, m, d)


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)


def add_months(d: date, n: int) -> date:
    """Add calendar months, clamping the day-of-month to the target month end."""
    y = d.year
    m0 = d.month - 1 + n
    y_target = y + m0 // 12
    m_target = m0 % 12 + 1
    last_day = calendar.monthrange(y_target, m_target)[1]
    return date(y_target, m_target, min(d.day, last_day))


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Sat=5, Sun=6


def adjust_following(d: date) -> date:
    """Following business day convention (weekend-only calendar)."""
    out = d
    while is_weekend(out):
        out = add_days(out, 1)
    return out


def days_between(d1: date, d2: date) -> int:
    """Actual day count between two dates."""
    return (d2 - d1).days


def act360(d1: date, d2: date) -> float:
    """ACT/360 year fraction."""
    return days_between(d1, d2) / 360.0


def act365(d1: date, d2: date) -> float:
    """ACT/365F year fraction (used as the model time axis)."""
    return days_between(d1, d2) / 365.0


def next_cds_date(d: date) -> date:
    """Next quarterly CDS/IMM roll date strictly after d (20 Mar/Jun/Sep/Dec)."""
    candidates = [date(yy, mm, 20) for yy in (d.year, d.year + 1) for mm in (3, 6, 9, 12)]
    for c in candidates:
        if c > d:
            return c
    return candidates[-1]


def prev_cds_date(d: date) -> date:
    """Previous quarterly CDS date on or before d."""
    candidates = [date(yy, mm, 20) for yy in (d.year - 1, d.year) for mm in (3, 6, 9, 12)]
    out = candidates[0]
    for c in candidates:
        if c <= d:
            out = c
    return out


def is_cds_date(d: date) -> bool:
    return d.day == 20 and d.month in (3, 6, 9, 12)


def standard_cds_maturity(trade_date: date, tenor_months: int) -> date:
    """Standard (unadjusted) CDS maturity, post-Dec-2015 semiannual roll.

    Since 20 Dec 2015 single-name maturities fall only on 20 Jun / 20 Dec,
    aligned with index maturities. The tenor is added to the semiannual
    anchor of the current roll window:

      - traded 20 Mar .. 19 Sep  -> anchor = 20 Jun of that year
      - traded 20 Sep .. 19 Mar  -> anchor = 20 Dec of the year of that 20 Sep

    E.g. a 5Y traded 2026-07-21 matures 2031-06-20 (same as the on-the-run
    index). Note the contract's actual remaining life therefore drifts from
    roughly tenor+0.25y down to tenor-0.25y as the roll window progresses.
    Maturities remain UNADJUSTED per the standard contract. Only tenors that
    are whole multiples of 6 months land on the semiannual grid; odd tenors
    (e.g. 9M) are non-standard and returned unsnapped.
    """
    md = (trade_date.month, trade_date.day)
    if (3, 20) <= md <= (9, 19):
        anchor = date(trade_date.year, 6, 20)
    elif md >= (9, 20):
        anchor = date(trade_date.year, 12, 20)
    else:  # 1 Jan .. 19 Mar
        anchor = date(trade_date.year - 1, 12, 20)
    return add_months(anchor, tenor_months)


@dataclass
class FeePeriod:
    """One accrual period of the fee (premium) leg."""

    accrual_start: date
    accrual_end: date  # unadjusted period end (coupon date)
    pay_date: date  # adjusted payment date
    accrual_time: float  # ACT/360 accrual fraction, incl. +1 day on last period
    is_last: bool


def build_fee_schedule(trade_date: date, maturity: date) -> list[FeePeriod]:
    """Generate the fee-leg schedule for a standard CDS.

    Accrual periods run between ADJUSTED (following) quarterly CDS dates,
    per the standard contract: each non-final period accrues from one
    adjusted payment date to the next, and the payment date IS the adjusted
    period end. The final period runs from the last adjusted roll to the
    UNADJUSTED maturity and accrues one extra day (maturity + 1) per ISDA
    convention. The first accrual start is the adjusted CDS date on or
    before the trade date (standard contracts accrue from the previous
    roll).
    """
    periods: list[FeePeriod] = []
    start_u = prev_cds_date(trade_date)
    end_u = next_cds_date(start_u)
    while end_u < maturity:
        a0 = adjust_following(start_u)
        a1 = adjust_following(end_u)
        periods.append(
            FeePeriod(
                accrual_start=a0,
                accrual_end=a1,
                pay_date=a1,
                accrual_time=act360(a0, a1),
                is_last=False,
            )
        )
        start_u = end_u
        end_u = next_cds_date(start_u)
    # last period: adjusted start -> unadjusted maturity, accrual gets +1 day
    a0 = adjust_following(start_u)
    periods.append(
        FeePeriod(
            accrual_start=a0,
            accrual_end=maturity,
            pay_date=adjust_following(maturity),
            accrual_time=act360(a0, add_days(maturity, 1)),
            is_last=True,
        )
    )
    return periods


def parse_iso(s: str) -> date:
    """Parse an ISO yyyy-mm-dd string into a date."""
    y, m, d = (int(x) for x in s.split("-"))
    return date(y, m, d)


def to_iso(d: date) -> str:
    return d.isoformat()
