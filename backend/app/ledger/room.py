"""Remaining contribution room (P9, invariant 7). Pure.

Remaining room for (account type, year) = the user-entered room for that year minus contributions of
that type dated on or after the entry's as-of date in that year. Contributions are approved plan
lines and manual contribution entries.

**Withdrawals never increase remaining room within a calendar year, for any account type.** They are
accepted as movements (so callers can pass a full history), but they never add room back. Room
changes only through a new user-entered annual figure. ``tests/unit/ledger/test_room.py`` holds the
property test.

No limit, carry-forward rule or tax constant lives here (invariant 6): the entered figure is the
whole truth.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal, localcontext
from enum import StrEnum

from pydantic import Field

from app.ledger.models import REGISTERED, AccountKind, Frozen, RoomStatus
from app.ledger.money import Money, PositiveMoney, ledger_context


class RoomEntry(Frozen):
    """Room as entered from CRA My Account."""

    kind: AccountKind
    year: int
    room: Money
    as_of: date
    source_note: str = Field(min_length=1)


class MovementType(StrEnum):
    CONTRIBUTION = "contribution"
    WITHDRAWAL = "withdrawal"


class RoomMovement(Frozen):
    kind: AccountKind
    on: date
    amount: PositiveMoney
    movement: MovementType


def remaining_room(entry: RoomEntry, movements: Iterable[RoomMovement]) -> Decimal:
    """Entered room minus contributions since the as-of date. May be negative (over-contributed)."""
    with localcontext(ledger_context()):
        used = sum(
            (
                m.amount
                for m in movements
                if m.kind is entry.kind
                and m.movement is MovementType.CONTRIBUTION
                and m.on >= entry.as_of
                and m.on.year == entry.year
            ),
            Decimal("0.00"),
        )
        return entry.room - used


def room_statuses(entries: Iterable[RoomEntry], movements: Iterable[RoomMovement], year: int) -> tuple[RoomStatus, ...]:
    """One status per registered type that has an entry for ``year``; types without one are stale."""
    history = tuple(movements)
    latest: dict[AccountKind, RoomEntry] = {}
    for entry in entries:
        if entry.year != year or entry.kind not in REGISTERED:
            continue
        current = latest.get(entry.kind)
        if current is None or entry.as_of > current.as_of:
            latest[entry.kind] = entry
    return tuple(
        RoomStatus(kind=kind, year=year, remaining=remaining_room(entry, history))
        for kind, entry in sorted(latest.items())
    )
