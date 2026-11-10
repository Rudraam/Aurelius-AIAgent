"""Exchange sessions (holidays, early closes) from the committed, generated calendar file. Fail closed.

Source: ``app/domain/data/exchange_sessions.json``, written by ``scripts/generate_exchange_calendars.py``
from the dev-only ``exchange_calendars`` package. The runtime reads only that file, so pandas never ships.

- Per exchange: XTSE for TSX listings, XNYS for NYSE/NASDAQ listings (same holidays and early closes).
- Times are Toronto wall-clock (see the generator: XNYS's New York times are asserted identical).
- Any date outside the file's coverage raises ``CalendarNotCovered``: callers must refuse, never guess.
  ``tests/unit/test_exchange_calendar.py`` fails once fewer than 90 days of coverage remain.
- The exchange comes from Questrade's ``listingMarket`` (``search_symbols``), cross-checked with the
  ``.TO`` suffix. Anything unknown (Pink Sheets, TSXV, ...) raises ``UnknownExchange``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, time
from enum import StrEnum
from functools import cache
from importlib.resources import files
from typing import Any, Final


class Exchange(StrEnum):
    XTSE = "XTSE"
    XNYS = "XNYS"


class CalendarNotCovered(LookupError):
    """The date is outside the generated calendar's coverage. Refuse; regenerate the file."""


class UnknownExchange(ValueError):
    """The listing market is not one Aurelius has a calendar for. Refuse."""


#: Questrade ``listingMarket`` values observed (TSX, NASDAQ on 2026-11-10) or assumed by analogy (NYSE;
#: confirm at M3). Unlisted values raise rather than default.
LISTING_MARKETS: Final[dict[str, Exchange]] = {
    "TSX": Exchange.XTSE,
    "NASDAQ": Exchange.XNYS,
    "NYSE": Exchange.XNYS,
}


def exchange_for(symbol: str, listing_market: str) -> Exchange:
    """Map a security to its calendar. ``.TO`` symbols must be TSX listings and vice versa."""
    exchange = LISTING_MARKETS.get(listing_market)
    if exchange is None:
        raise UnknownExchange(f"no calendar for listing market {listing_market!r} ({symbol})")
    if symbol.upper().endswith(".TO") != (exchange is Exchange.XTSE):
        raise UnknownExchange(f"symbol {symbol!r} does not match listing market {listing_market!r}")
    return exchange


@dataclass(frozen=True, slots=True)
class Session:
    day: date
    open: time
    close: time
    early_close: bool


@dataclass(frozen=True, slots=True)
class _ExchangeCalendar:
    regular_open: time
    regular_close: time
    closed_weekdays: frozenset[date]
    special_sessions: dict[date, tuple[time, time]]


@dataclass(frozen=True, slots=True)
class Calendars:
    source: str
    coverage_start: date
    coverage_end: date
    exchanges: dict[Exchange, _ExchangeCalendar]

    def session(self, exchange: Exchange, day: date) -> Session | None:
        """The trading session on ``day`` (Toronto date), or ``None`` if the exchange is closed."""
        if not self.coverage_start <= day <= self.coverage_end:
            raise CalendarNotCovered(
                f"{day} is outside the {exchange} calendar ({self.coverage_start}..{self.coverage_end})"
            )
        cal = self.exchanges[exchange]
        if day.weekday() >= 5 or day in cal.closed_weekdays:
            return None
        if day in cal.special_sessions:
            o, c = cal.special_sessions[day]
            return Session(day, o, c, early_close=c < cal.regular_close)
        return Session(day, cal.regular_open, cal.regular_close, early_close=False)


def _parse(raw: dict[str, Any]) -> Calendars:
    if raw.get("wall_clock_zone") != "America/Toronto":
        raise ValueError("calendar file must use America/Toronto wall-clock times")
    start = date.fromisoformat(raw["coverage"]["start"])
    end = date.fromisoformat(raw["coverage"]["end"])
    if end < start:
        raise ValueError("calendar coverage ends before it starts")
    exchanges: dict[Exchange, _ExchangeCalendar] = {}
    for code in Exchange:
        e = raw["exchanges"][code.value]
        closed = frozenset(date.fromisoformat(d) for d in e["closed_weekdays"])
        special = {
            date.fromisoformat(d): (time.fromisoformat(s["open"]), time.fromisoformat(s["close"]))
            for d, s in e["special_sessions"].items()
        }
        for d in closed | special.keys():
            if not start <= d <= end or d.weekday() >= 5:
                raise ValueError(f"{code}: {d} is outside coverage or not a weekday")
        if closed & special.keys():
            raise ValueError(f"{code}: a date is both closed and a special session")
        o, c = time.fromisoformat(e["regular_open"]), time.fromisoformat(e["regular_close"])
        if not o < c or any(not so < sc for so, sc in special.values()):
            raise ValueError(f"{code}: a session closes before it opens")
        exchanges[code] = _ExchangeCalendar(o, c, closed, special)
    return Calendars(str(raw["source"]), start, end, exchanges)


@cache
def calendars() -> Calendars:
    """Load and validate the committed calendar file once (raises on a malformed file: fail fast)."""
    text = files("app.domain").joinpath("data", "exchange_sessions.json").read_text(encoding="utf-8")
    return _parse(json.loads(text))
