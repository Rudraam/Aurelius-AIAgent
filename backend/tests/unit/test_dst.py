"""DST transitions for the Toronto time rules and the 16:30 snapshot job.

Fall back:      Sun 2026-11-01. 02:00 EDT -> 01:00 EST, so 01:00-01:59 happens twice.
Spring forward: Sun 2027-03-14. 02:00 EST -> 03:00 EDT, so 02:00-02:59 does not exist.

Every instant is written in UTC, with the Toronto wall-clock time in the comment. A fixed-offset
implementation (-05:00 or -04:00) gets at least one assertion in each group wrong.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.domain.exchange_calendar import Exchange
from app.domain.market_time import (
    DEFAULT_MAINTENANCE_WINDOW,
    TORONTO,
    LocalWindow,
    TimingViolation,
    check_order_timing,
    is_regular_session,
    to_toronto,
)
from app.jobs.schedules import daily_snapshot_trigger

W = DEFAULT_MAINTENANCE_WINDOW


def u(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def minutes(start: datetime, end: datetime) -> list[datetime]:
    out, t = [], start
    while t < end:
        out.append(t)
        t += timedelta(minutes=1)
    return out


# --------------------------------------------------------------------------- sanity: the zone really is IANA Toronto


def test_toronto_offsets_around_both_transitions():
    assert to_toronto(u(2026, 11, 1, 5, 30)).utcoffset() == timedelta(hours=-4)  # 01:30 EDT (first pass)
    assert to_toronto(u(2026, 11, 1, 6, 30)).utcoffset() == timedelta(hours=-5)  # 01:30 EST (second pass)
    assert to_toronto(u(2027, 3, 14, 6, 59)).utcoffset() == timedelta(hours=-5)  # 01:59 EST
    assert to_toronto(u(2027, 3, 14, 7, 0)).utcoffset() == timedelta(hours=-4)  # 03:00 EDT
    assert date(2026, 11, 1).weekday() == 6 and date(2027, 3, 14).weekday() == 6  # both are Sundays


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError, match="naive"):
        W.contains(datetime(2026, 11, 1, 2, 0))


# --------------------------------------------------------------------------- maintenance window 01:55-03:50 ET

FALL_BACK_CASES = [
    (u(2026, 11, 1, 5, 54), False, "01:54 EDT"),
    (u(2026, 11, 1, 5, 55), True, "01:55 EDT, first pass (fail-closed)"),
    (u(2026, 11, 1, 5, 59), True, "01:59 EDT"),
    (u(2026, 11, 1, 6, 0), False, "01:00 EST, the repeated hour begins"),
    (u(2026, 11, 1, 6, 54), False, "01:54 EST"),
    (u(2026, 11, 1, 6, 55), True, "01:55 EST, second pass"),
    (u(2026, 11, 1, 7, 0), True, "02:00 EST, Questrade's published start"),
    (u(2026, 11, 1, 8, 45), True, "03:45 EST, Questrade's published end"),
    (u(2026, 11, 1, 8, 49), True, "03:49 EST"),
    (u(2026, 11, 1, 8, 50), False, "03:50 EST, window closed"),
]

SPRING_FORWARD_CASES = [
    (u(2027, 3, 14, 6, 54), False, "01:54 EST"),
    (u(2027, 3, 14, 6, 55), True, "01:55 EST"),
    (u(2027, 3, 14, 6, 59), True, "01:59 EST"),
    (u(2027, 3, 14, 7, 0), True, "03:00 EDT (02:00-02:59 does not exist)"),
    (u(2027, 3, 14, 7, 49), True, "03:49 EDT"),
    (u(2027, 3, 14, 7, 50), False, "03:50 EDT; a fixed -05:00 would say 02:50 and stay closed"),
]


@pytest.mark.parametrize(("instant", "inside", "local"), FALL_BACK_CASES, ids=[c[2] for c in FALL_BACK_CASES])
def test_maintenance_window_fall_back(instant, inside, local):
    assert W.contains(instant) is inside, local


@pytest.mark.parametrize(("instant", "inside", "local"), SPRING_FORWARD_CASES, ids=[c[2] for c in SPRING_FORWARD_CASES])
def test_maintenance_window_spring_forward(instant, inside, local):
    assert W.contains(instant) is inside, local


@pytest.mark.parametrize(
    ("day", "qt_start_utc", "qt_end_utc"),
    [
        (date(2026, 11, 1), u(2026, 11, 1, 7, 0), u(2026, 11, 1, 8, 45)),  # 02:00-03:45 EST
        (date(2027, 3, 14), u(2027, 3, 14, 7, 0), u(2027, 3, 14, 7, 45)),  # 03:00-03:45 EDT (02:xx skipped)
        (date(2026, 7, 1), u(2026, 7, 1, 6, 0), u(2026, 7, 1, 7, 45)),  # ordinary summer day, EDT
        (date(2027, 1, 15), u(2027, 1, 15, 7, 0), u(2027, 1, 15, 8, 45)),  # ordinary winter day, EST
    ],
    ids=["fall-back", "spring-forward", "summer", "winter"],
)
def test_our_window_always_covers_questrades_published_window(day, qt_start_utc, qt_end_utc):
    """Every minute Questrade refuses order tools (02:00-03:45 ET), we refuse too, DST or not."""
    uncovered = [t for t in minutes(qt_start_utc, qt_end_utc) if not W.contains(t)]
    assert uncovered == [], f"{day}: gap at {[to_toronto(t).isoformat() for t in uncovered[:3]]}"


def test_spring_forward_window_has_no_hole():
    inside = [W.contains(t) for t in minutes(u(2027, 3, 14, 6, 55), u(2027, 3, 14, 7, 50))]
    assert all(inside) and len(inside) == 55  # 01:55 EST to 03:50 EDT is 55 real minutes


def test_fall_back_window_real_duration():
    inside = [t for t in minutes(u(2026, 11, 1, 5, 0), u(2026, 11, 1, 10, 0)) if W.contains(t)]
    # 5 min (01:55-02:00 EDT) + 115 min (01:55-03:50 EST)
    assert len(inside) == 120


def test_window_parsing_and_wraparound():
    assert LocalWindow.parse("01:55-03:50") == LocalWindow(time(1, 55), time(3, 50))
    wrap = LocalWindow.parse("23:30-00:30")
    assert wrap.contains(u(2026, 11, 2, 4, 45))  # 23:45 EST, Sunday 2026-11-01
    assert wrap.contains(u(2026, 11, 2, 5, 15))  # 00:15 EST
    assert not wrap.contains(u(2026, 11, 2, 5, 45))  # 00:45 EST
    with pytest.raises(ValueError):
        LocalWindow.parse("1:55 to 3:50")


# --------------------------------------------------------------------------- market hours 09:30-16:00 ET, Mon-Fri


@pytest.mark.parametrize("day", [date(2026, 11, 1), date(2027, 3, 14)], ids=["fall-back-sunday", "spring-sunday"])
@pytest.mark.parametrize("exchange", list(Exchange))
def test_market_closed_all_day_on_transition_sundays(day, exchange):
    start = datetime.combine(day, time(0, 0), TORONTO).astimezone(UTC)
    assert not any(is_regular_session(t, exchange) for t in minutes(start, start + timedelta(hours=24)))


MARKET_CASES = [
    # last trading day before fall back: EDT
    (u(2026, 10, 30, 13, 29), False, "Fri 09:29 EDT"),
    (u(2026, 10, 30, 13, 30), True, "Fri 09:30 EDT"),
    (u(2026, 10, 30, 19, 59), True, "Fri 15:59 EDT"),
    (u(2026, 10, 30, 20, 0), False, "Fri 16:00 EDT"),
    # first trading day after fall back: EST. A fixed -04:00 would open an hour early.
    (u(2026, 11, 2, 13, 30), False, "Mon 08:30 EST"),
    (u(2026, 11, 2, 14, 29), False, "Mon 09:29 EST"),
    (u(2026, 11, 2, 14, 30), True, "Mon 09:30 EST"),
    (u(2026, 11, 2, 20, 59), True, "Mon 15:59 EST"),
    (u(2026, 11, 2, 21, 0), False, "Mon 16:00 EST"),
    # last trading day before spring forward: EST
    (u(2027, 3, 12, 14, 30), True, "Fri 09:30 EST"),
    (u(2027, 3, 12, 20, 59), True, "Fri 15:59 EST"),
    # first trading day after spring forward: EDT. A fixed -05:00 would open an hour late.
    (u(2027, 3, 15, 13, 29), False, "Mon 09:29 EDT"),
    (u(2027, 3, 15, 13, 30), True, "Mon 09:30 EDT"),
    (u(2027, 3, 15, 19, 59), True, "Mon 15:59 EDT"),
    (u(2027, 3, 15, 20, 0), False, "Mon 16:00 EDT"),
]


@pytest.mark.parametrize(("instant", "open_", "local"), MARKET_CASES, ids=[c[2] for c in MARKET_CASES])
@pytest.mark.parametrize("exchange", list(Exchange))
def test_market_hours_around_dst(instant, open_, local, exchange):
    # none of these dates is a holiday on either exchange; holidays are covered in test_exchange_calendar
    assert is_regular_session(instant, exchange) is open_, local


# --------------------------------------------------------------------------- order timing guardrail


@pytest.mark.parametrize(
    ("order_type", "instant", "expected"),
    [
        ("limit", u(2026, 11, 1, 5, 57), TimingViolation.MAINTENANCE_WINDOW),  # 01:57 EDT, first pass
        ("limit", u(2026, 11, 1, 6, 30), None),  # 01:30 EST, repeated hour
        ("limit", u(2026, 11, 1, 7, 30), TimingViolation.MAINTENANCE_WINDOW),  # 02:30 EST
        ("limit", u(2027, 3, 14, 7, 10), TimingViolation.MAINTENANCE_WINDOW),  # 03:10 EDT
        ("limit", u(2027, 3, 14, 16, 0), None),  # Sunday noon: limit allowed
        ("market", u(2027, 3, 14, 16, 0), TimingViolation.MARKET_CLOSED_FOR_MARKET_ORDER),  # Sunday noon
        ("market", u(2026, 11, 2, 13, 45), TimingViolation.MARKET_CLOSED_FOR_MARKET_ORDER),  # Mon 08:45 EST
        ("market", u(2026, 11, 2, 14, 45), None),  # Mon 09:45 EST
        ("market", u(2027, 3, 15, 13, 45), None),  # Mon 09:45 EDT
    ],
)
@pytest.mark.parametrize("exchange", list(Exchange))
def test_order_timing_across_dst(order_type, instant, expected, exchange):
    assert check_order_timing(order_type, instant, exchange) == expected


# ---------------------------------------------------------------- 16:30 snapshot job (real APScheduler trigger)


def fires(start: datetime, end: datetime) -> list[datetime]:
    trig, out, prev = daily_snapshot_trigger(), [], None
    now = start
    while True:
        nxt = trig.get_next_fire_time(prev, now)
        if nxt is None or nxt >= end:
            return out
        out.append(nxt)
        prev, now = nxt, nxt + timedelta(seconds=1)


def test_snapshot_does_not_run_on_transition_sundays():
    for day in (date(2026, 11, 1), date(2027, 3, 14)):
        start = datetime.combine(day, time(0, 0), TORONTO)
        assert fires(start, start + timedelta(days=1)) == []


def test_snapshot_across_fall_back_is_1630_local_and_shifts_utc():
    runs = fires(u(2026, 10, 26, 0), u(2026, 11, 7, 0))
    assert len(runs) == 10  # Mon-Fri x 2 weeks, no duplicates from the repeated hour
    assert all(to_toronto(r).time() == time(16, 30) and to_toronto(r).weekday() < 5 for r in runs)
    utc_hours = {r.astimezone(TORONTO).date(): r.astimezone(UTC).hour for r in runs}
    assert utc_hours[date(2026, 10, 30)] == 20  # Fri 16:30 EDT
    assert utc_hours[date(2026, 11, 2)] == 21  # Mon 16:30 EST


def test_snapshot_across_spring_forward_is_1630_local_and_shifts_utc():
    runs = fires(u(2027, 3, 8, 0), u(2027, 3, 20, 0))
    assert len(runs) == 10
    assert all(to_toronto(r).time() == time(16, 30) for r in runs)
    utc_hours = {r.astimezone(TORONTO).date(): r.astimezone(UTC).hour for r in runs}
    assert utc_hours[date(2027, 3, 12)] == 21  # Fri 16:30 EST
    assert utc_hours[date(2027, 3, 15)] == 20  # Mon 16:30 EDT


def test_snapshot_next_fire_over_each_transition_weekend():
    trig = daily_snapshot_trigger()
    after_fri_fall = trig.get_next_fire_time(None, u(2026, 10, 30, 20, 31))  # Fri 16:31 EDT
    assert after_fri_fall == u(2026, 11, 2, 21, 30)  # Mon 16:30 EST
    after_fri_spring = trig.get_next_fire_time(None, u(2027, 3, 12, 21, 31))  # Fri 16:31 EST
    assert after_fri_spring == u(2027, 3, 15, 20, 30)  # Mon 16:30 EDT
