"""Pure date math: paydays and bill due dates. Dates only, no clock and no time zones (P15).

- **Paydays** are generated from the schedule's nominal dates. A payday that falls on a weekend or a
  user-entered bank holiday moves to the previous business day (bank holidays are data, not the
  exchange calendar, because bank and exchange holidays differ).
- **Day of month** is clamped: a day-31 rule falls on the last day of shorter months (Feb 28/29).
- **Bill occurrences** start at the bill's anchor (its next due date); none are generated before it.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Iterator
from datetime import date, timedelta

from app.ledger.models import Bill, BillFrequency, IncomeSchedule, PayFrequency

_DAY = timedelta(days=1)
#: How far beyond a range nominal dates are generated, so adjustments into the range are seen.
_PAD = timedelta(days=21)

PAYDAYS_PER_YEAR: dict[PayFrequency, int] = {
    PayFrequency.WEEKLY: 52,
    PayFrequency.BIWEEKLY: 26,
    PayFrequency.SEMIMONTHLY: 24,
    PayFrequency.MONTHLY: 12,
}


def clamp_day(year: int, month: int, day: int) -> date:
    """``year-month-day``, with ``day`` clamped to the month's last day."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _month_index(d: date) -> int:
    return d.year * 12 + (d.month - 1)


def _months(start: date, end: date) -> Iterator[tuple[int, int]]:
    """Every (year, month) from start's month to end's month inclusive."""
    for index in range(_month_index(start), _month_index(end) + 1):
        yield divmod(index, 12)[0], divmod(index, 12)[1] + 1


def previous_business_day(day: date, holidays: Iterable[date]) -> date:
    """``day`` itself if it is a business day, else the closest earlier one."""
    closed = frozenset(holidays)
    while day.weekday() >= 5 or day in closed:
        day -= _DAY
    return day


def _nominal_paydays(schedule: IncomeSchedule, start: date, end: date) -> Iterator[date]:
    """Unadjusted paydays covering [start, end)."""
    if schedule.frequency in (PayFrequency.WEEKLY, PayFrequency.BIWEEKLY):
        step = 7 if schedule.frequency is PayFrequency.WEEKLY else 14
        first = (start - schedule.anchor).days // step
        last = (end - schedule.anchor).days // step + 1
        for k in range(first, last + 1):
            yield schedule.anchor + timedelta(days=step * k)
        return
    for year, month in _months(start, end):
        if schedule.frequency is PayFrequency.MONTHLY:
            yield clamp_day(year, month, schedule.anchor.day)
        else:
            for day in schedule.semimonthly_days:
                yield clamp_day(year, month, day)


def paydays(schedule: IncomeSchedule, holidays: Iterable[date], start: date, end: date) -> list[date]:
    """Adjusted paydays in [start, end), sorted and unique."""
    closed = frozenset(holidays)
    adjusted = {previous_business_day(d, closed) for d in _nominal_paydays(schedule, start - _PAD, end + _PAD)}
    return sorted(d for d in adjusted if start <= d < end)


def next_payday_after(schedule: IncomeSchedule, holidays: Iterable[date], day: date) -> date:
    """The first adjusted payday strictly after ``day``."""
    closed = frozenset(holidays)
    start = day + _DAY
    found = paydays(schedule, closed, start, start + timedelta(days=62))
    if not found:  # pragma: no cover - every schedule pays at least monthly
        raise ValueError(f"no payday within 62 days after {day}")
    return found[0]


def paydays_in_month(schedule: IncomeSchedule, holidays: Iterable[date], year: int, month: int) -> int:
    first = date(year, month, 1)
    after = clamp_day(year, month, 31) + _DAY
    return len(paydays(schedule, holidays, first, after))


def _bill_step_months(bill: Bill) -> int:
    if bill.frequency is BillFrequency.MONTHLY:
        return 1
    if bill.frequency is BillFrequency.ANNUAL:
        return 12
    assert bill.interval_months is not None  # enforced by Bill's validator
    return bill.interval_months


def bill_due_dates(bill: Bill, start: date, end: date) -> list[date]:
    """Due dates of ``bill`` in [start, end), never before its anchor."""
    lower = max(start, bill.anchor)
    if lower >= end:
        return []
    if bill.frequency is BillFrequency.BIWEEKLY:
        k = max(0, -(-(lower - bill.anchor).days // 14))
        out = []
        d = bill.anchor + timedelta(days=14 * k)
        while d < end:
            if d >= lower:
                out.append(d)
            d += timedelta(days=14)
        return out
    step = _bill_step_months(bill)
    phase = _month_index(bill.anchor) % step
    out = []
    for year, month in _months(lower, end):
        if (year * 12 + month - 1) % step != phase:
            continue
        d = clamp_day(year, month, bill.due_day)
        if lower <= d < end:
            out.append(d)
    return out


def next_due_on_or_after(bill: Bill, day: date) -> date:
    """The first due date of ``bill`` on or after ``day``."""
    span = (
        timedelta(days=31 * _bill_step_months(bill) + 31)
        if bill.frequency is not BillFrequency.BIWEEKLY
        else timedelta(days=15)
    )
    start = max(day, bill.anchor)
    found = bill_due_dates(bill, start, start + span)
    if not found:  # pragma: no cover - the span always covers one period
        raise ValueError(f"no due date for bill {bill.id} within {span.days} days of {start}")
    return found[0]
