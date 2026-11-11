"""Date edge cases: month ends, the 31st, leap years, weekend paydays, bank holidays (spec section 9)."""

from __future__ import annotations

from datetime import date

import pytest

from app.ledger.models import BillFrequency, IncomeSchedule, PayFrequency
from app.ledger.schedule import (
    bill_due_dates,
    clamp_day,
    next_due_on_or_after,
    next_payday_after,
    paydays,
    paydays_in_month,
    previous_business_day,
)
from tests.unit.ledger.factories import bill


def sched(freq: PayFrequency, anchor: date, days: tuple[int, int] = (15, 31)) -> IncomeSchedule:
    return IncomeSchedule(frequency=freq, anchor=anchor, semimonthly_days=days)


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [
        (2027, 2, date(2027, 2, 28)),
        (2028, 2, date(2028, 2, 29)),
        (2100, 2, date(2100, 2, 28)),
        (2000, 2, date(2000, 2, 29)),
        (2027, 4, date(2027, 4, 30)),
        (2027, 12, date(2027, 12, 31)),
    ],
)
def test_clamp_day_31(year: int, month: int, expected: date) -> None:
    assert clamp_day(year, month, 31) == expected


def test_weekend_and_holiday_move_to_previous_business_day() -> None:
    assert previous_business_day(date(2027, 5, 1), ()) == date(2027, 4, 30)  # Saturday -> Friday
    assert previous_business_day(date(2027, 5, 2), ()) == date(2027, 4, 30)  # Sunday -> Friday
    # Monday holiday after a weekend: back to Friday.
    assert previous_business_day(date(2027, 5, 24), (date(2027, 5, 24),)) == date(2027, 5, 21)
    # Friday holiday: Thursday.
    assert previous_business_day(date(2027, 4, 2), (date(2027, 4, 2),)) == date(2027, 4, 1)


def test_semimonthly_15th_and_last_with_weekend_adjustment() -> None:
    s = sched(PayFrequency.SEMIMONTHLY, date(2027, 1, 15))
    got = paydays(s, (), date(2027, 1, 1), date(2027, 4, 1))
    # 2027-01-31 is a Sunday -> Fri 01-29; 02-28 Sunday -> Fri 02-26; 03-31 Wednesday.
    assert got == [
        date(2027, 1, 15),
        date(2027, 1, 29),
        date(2027, 2, 15),
        date(2027, 2, 26),
        date(2027, 3, 15),
        date(2027, 3, 31),
    ]


def test_semimonthly_leap_february() -> None:
    s = sched(PayFrequency.SEMIMONTHLY, date(2028, 1, 15))
    assert paydays(s, (), date(2028, 2, 1), date(2028, 3, 1)) == [date(2028, 2, 15), date(2028, 2, 29)]


def test_monthly_on_31st_clamps_and_adjusts() -> None:
    s = sched(PayFrequency.MONTHLY, date(2027, 1, 31))
    got = paydays(s, (), date(2027, 1, 1), date(2027, 7, 1))
    # Jan 31 Sun -> Jan 29; Feb 28 Sun -> Feb 26; Mar 31 Wed; Apr 30 Fri; May 31 Mon; Jun 30 Wed.
    assert got == [
        date(2027, 1, 29),
        date(2027, 2, 26),
        date(2027, 3, 31),
        date(2027, 4, 30),
        date(2027, 5, 31),
        date(2027, 6, 30),
    ]


def test_adjustment_can_pull_a_payday_into_the_previous_month() -> None:
    s = sched(PayFrequency.MONTHLY, date(2027, 5, 1))  # nominal day 1
    # 2027-05-01 is a Saturday -> Friday 2027-04-30, which belongs to April.
    assert paydays(s, (), date(2027, 4, 1), date(2027, 5, 1)) == [date(2027, 4, 1), date(2027, 4, 30)]
    assert paydays_in_month(s, (), 2027, 5) == 0


def test_biweekly_three_paycheque_month() -> None:
    s = sched(PayFrequency.BIWEEKLY, date(2027, 1, 1))
    assert paydays_in_month(s, (), 2027, 1) == 3  # Jan 1, 15, 29
    assert paydays_in_month(s, (), 2027, 2) == 2


def test_bank_holiday_payday_moves_earlier() -> None:
    s = sched(PayFrequency.BIWEEKLY, date(2027, 1, 8))
    holiday = date(2027, 1, 22)
    assert next_payday_after(s, (holiday,), date(2027, 1, 8)) == date(2027, 1, 21)
    assert next_payday_after(s, (), date(2027, 1, 8)) == date(2027, 1, 22)


def test_next_payday_is_strictly_after() -> None:
    s = sched(PayFrequency.WEEKLY, date(2027, 1, 1))
    assert next_payday_after(s, (), date(2027, 1, 1)) == date(2027, 1, 8)
    assert next_payday_after(s, (), date(2026, 12, 31)) == date(2027, 1, 1)


def test_year_boundary_biweekly() -> None:
    s = sched(PayFrequency.BIWEEKLY, date(2027, 1, 8))
    assert next_payday_after(s, (), date(2026, 12, 25)) == date(2027, 1, 8)
    assert paydays(s, (), date(2026, 12, 1), date(2027, 1, 1)) == [date(2026, 12, 11), date(2026, 12, 25)]


def test_monthly_bill_on_31st() -> None:
    b = bill("rent", "100.00", date(2027, 1, 31), day_of_month=31)
    assert bill_due_dates(b, date(2027, 1, 1), date(2027, 5, 1)) == [
        date(2027, 1, 31),
        date(2027, 2, 28),
        date(2027, 3, 31),
        date(2027, 4, 30),
    ]
    leap = bill("rent", "100.00", date(2028, 1, 31), day_of_month=31)
    assert bill_due_dates(leap, date(2028, 2, 1), date(2028, 3, 1)) == [date(2028, 2, 29)]


def test_annual_bill_on_feb_29() -> None:
    b = bill("domain", "20.00", date(2028, 2, 29), frequency=BillFrequency.ANNUAL)
    assert bill_due_dates(b, date(2028, 1, 1), date(2031, 1, 1)) == [
        date(2028, 2, 29),
        date(2029, 2, 28),
        date(2030, 2, 28),
    ]


def test_every_n_months_and_anchor_is_first() -> None:
    b = bill("water", "80.00", date(2027, 2, 10), frequency=BillFrequency.EVERY_N_MONTHS, interval_months=3)
    assert bill_due_dates(b, date(2027, 1, 1), date(2028, 1, 1)) == [
        date(2027, 2, 10),
        date(2027, 5, 10),
        date(2027, 8, 10),
        date(2027, 11, 10),
    ]


def test_biweekly_bill() -> None:
    b = bill("daycare", "300.00", date(2027, 1, 4), frequency=BillFrequency.BIWEEKLY)
    assert bill_due_dates(b, date(2027, 1, 5), date(2027, 2, 15)) == [date(2027, 1, 18), date(2027, 2, 1)]
    assert bill_due_dates(b, date(2026, 12, 1), date(2027, 1, 4)) == []  # never before the anchor


def test_next_due_on_or_after() -> None:
    b = bill("insurance", "600.00", date(2027, 6, 1), frequency=BillFrequency.ANNUAL)
    assert next_due_on_or_after(b, date(2027, 1, 8)) == date(2027, 6, 1)
    assert next_due_on_or_after(b, date(2027, 6, 1)) == date(2027, 6, 1)
    assert next_due_on_or_after(b, date(2027, 6, 2)) == date(2028, 6, 1)
