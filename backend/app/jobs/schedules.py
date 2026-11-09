"""Scheduler triggers. Always built with the IANA Toronto zone so APScheduler handles DST."""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger

from app.domain.market_time import TORONTO

SNAPSHOT_HOUR = 16
SNAPSHOT_MINUTE = 30


def daily_snapshot_trigger() -> CronTrigger:
    """Mon-Fri 16:30 America/Toronto (spec section 13): 21:30 UTC in winter, 20:30 UTC in summer."""
    return CronTrigger(day_of_week="mon-fri", hour=SNAPSHOT_HOUR, minute=SNAPSHOT_MINUTE, timezone=TORONTO)
