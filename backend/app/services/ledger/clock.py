"""From an instant to a Toronto calendar date (P15). The only place the ledger services meet DST."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from app.domain.market_time import to_toronto

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_utc(now: datetime) -> datetime:
    """``now`` must be an aware instant at UTC offset zero; services store it as given."""
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be an aware UTC datetime")
    return now


def toronto_today(now: datetime) -> date:
    """The America/Toronto calendar date at the aware instant ``now``."""
    return to_toronto(require_utc(now)).date()
