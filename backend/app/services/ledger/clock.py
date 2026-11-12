"""From an instant to a Toronto calendar date (P15). The only place the ledger services meet DST."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

from app.domain.market_time import to_toronto

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def toronto_today(now: datetime) -> date:
    """The America/Toronto calendar date at the aware instant ``now``."""
    return to_toronto(now).date()
