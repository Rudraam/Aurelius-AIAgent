"""Exchange calendars (amendment A9): holidays, early closes, fail-closed coverage, no pandas at runtime."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from app.domain.exchange_calendar import (
    CalendarNotCovered,
    Exchange,
    UnknownExchange,
    _parse,
    calendars,
    exchange_for,
)
from app.domain.market_time import TORONTO, TimingViolation, check_order_timing, is_regular_session

BACKEND = Path(__file__).resolve().parents[2]
DATA = BACKEND / "app" / "domain" / "data" / "exchange_sessions.json"
MIN_COVERAGE = timedelta(days=90)

XTSE, XNYS = Exchange.XTSE, Exchange.XNYS


def at(d: date, hh: int, mm: int) -> datetime:
    """A Toronto wall-clock time on ``d`` as a UTC instant."""
    return datetime.combine(d, time(hh, mm), TORONTO).astimezone(UTC)


# --------------------------------------------------------------------------- coverage: forces regeneration


def test_coverage_has_at_least_90_days_left():
    today = datetime.now(TORONTO).date()
    end = calendars().coverage_end
    assert end - today >= MIN_COVERAGE, (
        f"exchange calendar coverage ends {end}; regenerate: uv run python scripts/generate_exchange_calendars.py"
    )


@pytest.mark.filterwarnings("ignore:Series.values returning an ndarray")  # inside exchange_calendars
def test_committed_file_matches_the_generator():
    pytest.importorskip("exchange_calendars")  # dev-only dependency; always present in CI
    sys.path.insert(0, str(BACKEND / "scripts"))
    try:
        import generate_exchange_calendars as gen
    finally:
        sys.path.remove(str(BACKEND / "scripts"))
    assert DATA.read_text(encoding="utf-8") == gen.render(), "hand-edited or stale; regenerate the file"


# --------------------------------------------------------------------------- known dates (checked independently)

# Every weekday on which the exchange is closed in 2026, verified against TSX/NYSE published schedules.
XTSE_2026 = {"01-01", "02-16", "04-03", "05-18", "07-01", "08-03", "09-07", "10-12", "12-25", "12-28"}
XNYS_2026 = {"01-01", "01-19", "02-16", "04-03", "05-25", "06-19", "07-03", "09-07", "11-26", "12-25"}


@pytest.mark.parametrize(("exchange", "expected"), [(XTSE, XTSE_2026), (XNYS, XNYS_2026)])
def test_2026_holidays_exactly(exchange, expected):
    cal = calendars()
    closed = {
        f"{d:%m-%d}"
        for d in (date(2026, 1, 1) + timedelta(days=i) for i in range(365))
        if d.weekday() < 5 and cal.session(exchange, d) is None
    }
    assert closed == expected


@pytest.mark.parametrize(
    ("exchange", "day", "close"),
    [
        (XNYS, date(2026, 11, 27), time(13, 0)),  # day after US Thanksgiving
        (XNYS, date(2026, 12, 24), time(13, 0)),
        (XTSE, date(2026, 12, 24), time(13, 0)),
        (XTSE, date(2026, 11, 27), time(16, 0)),  # a normal day on the TSX
    ],
)
def test_early_closes(exchange, day, close):
    s = calendars().session(exchange, day)
    assert s is not None and s.close == close and s.early_close is (close != time(16, 0))
    closing = at(day, close.hour, close.minute)
    assert is_regular_session(closing - timedelta(minutes=1), exchange)
    assert not is_regular_session(closing, exchange)


def test_canadian_and_us_thanksgiving_differ():
    assert not is_regular_session(at(date(2026, 10, 12), 11, 0), XTSE)
    assert is_regular_session(at(date(2026, 10, 12), 11, 0), XNYS)
    assert is_regular_session(at(date(2026, 11, 26), 11, 0), XTSE)
    assert not is_regular_session(at(date(2026, 11, 26), 11, 0), XNYS)


def test_observed_holidays_2027():
    cal = calendars()
    assert cal.session(XTSE, date(2027, 12, 27)) is None  # Christmas (Sat) observed Mon
    assert cal.session(XTSE, date(2027, 12, 28)) is None  # Boxing Day (Sun) observed Tue
    assert cal.session(XNYS, date(2027, 12, 24)) is None  # Christmas (Sat) observed Fri
    assert cal.session(XNYS, date(2027, 7, 5)) is None  # Independence Day (Sun) observed Mon


# --------------------------------------------------------------------------- fail closed


def test_dates_outside_coverage_raise():
    cal = calendars()
    for d in (cal.coverage_start - timedelta(days=1), cal.coverage_end + timedelta(days=1)):
        with pytest.raises(CalendarNotCovered):
            cal.session(XTSE, d)


def test_market_order_beyond_coverage_is_blocked_limit_is_not():
    beyond = calendars().coverage_end + timedelta(days=7)
    while beyond.weekday() >= 5:
        beyond += timedelta(days=1)
    instant = at(beyond, 11, 0)
    assert check_order_timing("market", instant, XTSE) is TimingViolation.CALENDAR_NOT_COVERED
    assert check_order_timing("limit", instant, XTSE) is None


def test_market_order_on_a_holiday_is_blocked():
    assert (
        check_order_timing("market", at(date(2026, 7, 1), 11, 0), XTSE)
        is TimingViolation.MARKET_CLOSED_FOR_MARKET_ORDER
    )
    assert check_order_timing("market", at(date(2026, 7, 1), 11, 0), XNYS) is None


# --------------------------------------------------------------------------- listing market -> exchange


@pytest.mark.parametrize(
    ("symbol", "market", "expected"),
    [("SHOP.TO", "TSX", XTSE), ("SHOP", "NASDAQ", XNYS), ("XEQT.TO", "TSX", XTSE), ("IBM", "NYSE", XNYS)],
)
def test_exchange_for_known_listings(symbol, market, expected):
    assert exchange_for(symbol, market) is expected


@pytest.mark.parametrize(
    ("symbol", "market"),
    [("SRHGF", "Pink Sheets"), ("SHOP", "TSX"), ("SHOP.TO", "NASDAQ"), ("ABC.V", "TSXV"), ("X", "")],
)
def test_exchange_for_refuses_unknown_or_mismatched(symbol, market):
    with pytest.raises(UnknownExchange):
        exchange_for(symbol, market)


def test_exchange_for_real_search_results():
    path = BACKEND / "tests" / "fixtures" / "questrade" / "market" / "search_symbols.json"
    results = json.loads(path.read_text(encoding="utf-8"))["response"]["results"]
    mapped = {}
    for r in results:
        try:
            mapped[r["symbol"]] = exchange_for(r["symbol"], r["listingMarket"])
        except UnknownExchange:
            mapped[r["symbol"]] = None
    assert mapped == {"SHOP": XNYS, "SHOP.TO": XTSE, "SRHGF": None, "SRGHY": None, "GWKSY": None}


# --------------------------------------------------------------------------- file validation


def _raw() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(wall_clock_zone="America/New_York"),
        lambda r: r["coverage"].update(end="2025-01-01"),
        lambda r: r["exchanges"]["XTSE"]["closed_weekdays"].append("2026-11-07"),  # a Saturday
        lambda r: r["exchanges"]["XTSE"]["closed_weekdays"].append("2030-01-02"),  # beyond coverage
        lambda r: r["exchanges"]["XTSE"]["closed_weekdays"].append("2026-12-24"),  # also a special session
        lambda r: r["exchanges"]["XNYS"]["special_sessions"].update(
            {"2026-11-30": {"open": "13:00", "close": "09:30"}}
        ),
        lambda r: r["exchanges"].pop("XNYS"),
    ],
)
def test_malformed_files_are_rejected(mutate):
    raw = copy.deepcopy(_raw())
    mutate(raw)
    with pytest.raises((ValueError, KeyError)):
        _parse(raw)


# --------------------------------------------------------------------------- runtime boundary


def test_runtime_never_imports_pandas_or_exchange_calendars():
    code = (
        "import sys, app.domain.market_time as m, app.domain.exchange_calendar as c; c.calendars(); "
        "bad = {'pandas', 'numpy', 'exchange_calendars'} & set(sys.modules); assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], cwd=BACKEND, check=True)  # noqa: S603
    pattern = re.compile(r"^\s*(import|from)\s+(exchange_calendars|pandas|numpy)\b", re.M)
    offenders = [p for p in (BACKEND / "app").rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
