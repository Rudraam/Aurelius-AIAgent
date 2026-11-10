"""Toronto wall-clock rules: maintenance window, exchange sessions, order timing.

The only place Aurelius names the Toronto time zone. Rules are evaluated on Toronto wall-clock time
via IANA ``America/Toronto`` (tzdata), never a fixed UTC offset: ET is -05:00 in winter and -04:00 in
summer, and a fixed offset is wrong for half the year. ``tests/unit/test_no_fixed_offsets.py``
enforces this.

DST semantics of a wall-clock window ``[start, end)`` (both instants converted to Toronto local time):
- Fall back (first Sunday of Nov, 01:00-01:59 occurs twice): a time in the repeated hour is inside
  the window on both passes. For 01:55-03:50 that means 01:55-01:59 EDT counts, then the second
  01:00-01:54 (EST) does not, then 01:55 EST to 03:50 EST does. Blocking the extra five minutes is
  fail-closed and harmless.
- Spring forward (second Sunday of Mar, 02:00-02:59 does not exist): the window runs from 01:55 EST
  straight on through 03:00 to 03:50 EDT, so no gap opens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo

from app.domain.exchange_calendar import CalendarNotCovered, Exchange, calendars

TORONTO: Final = ZoneInfo("America/Toronto")  # fails at import if tzdata is missing: fail fast


def require_aware(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("naive datetime: time rules need an aware instant (store and pass UTC)")
    return instant


def to_toronto(instant: datetime) -> datetime:
    return require_aware(instant).astimezone(TORONTO)


@dataclass(frozen=True, slots=True)
class LocalWindow:
    """A daily Toronto wall-clock window ``[start, end)``. ``start > end`` wraps past midnight."""

    start: time
    end: time

    @classmethod
    def parse(cls, spec: str) -> LocalWindow:
        """Parse ``"HH:MM-HH:MM"`` (the AURELIUS_MAINTENANCE_WINDOW_ET format)."""
        try:
            a, b = (part.strip() for part in spec.split("-"))
            return cls(time.fromisoformat(a), time.fromisoformat(b))
        except ValueError as e:
            raise ValueError(f"invalid window {spec!r}; expected HH:MM-HH:MM") from e

    def contains(self, instant: datetime) -> bool:
        local = to_toronto(instant).time().replace(tzinfo=None)
        if self.start <= self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end


#: Buffer around Questrade's published 02:00-03:45 ET. Configurable because the schedule can change.
DEFAULT_MAINTENANCE_WINDOW: Final = LocalWindow.parse("01:55-03:50")


def is_regular_session(instant: datetime, exchange: Exchange) -> bool:
    """True inside the exchange's session on that Toronto date: ``open <= local time < close``, using the
    generated calendar (holidays, early closes). Raises ``CalendarNotCovered`` outside the file's coverage."""
    local = to_toronto(instant)
    session = calendars().session(exchange, local.date())
    return session is not None and session.open <= local.time().replace(tzinfo=None) < session.close


class TimingViolation(StrEnum):
    MAINTENANCE_WINDOW = "maintenance_window"
    MARKET_CLOSED_FOR_MARKET_ORDER = "market_closed_for_market_order"
    CALENDAR_NOT_COVERED = "calendar_not_covered"


def check_order_timing(
    order_type: str, instant: datetime, exchange: Exchange, window: LocalWindow = DEFAULT_MAINTENANCE_WINDOW
) -> TimingViolation | None:
    """Guardrail rule (spec section 9): every order type is refused inside the maintenance window; market
    orders additionally need a session on the exchange's calendar (fail closed beyond its coverage);
    limit orders are allowed any other time."""
    if window.contains(instant):
        return TimingViolation.MAINTENANCE_WINDOW
    if order_type == "market":
        try:
            if not is_regular_session(instant, exchange):
                return TimingViolation.MARKET_CLOSED_FOR_MARKET_ORDER
        except CalendarNotCovered:
            return TimingViolation.CALENDAR_NOT_COVERED
    return None
