"""Append-only ledger records: contribution room, room movements, goal adjustments, policy versions.

Rows here are history (P29): they are only ever inserted, and a database trigger rejects updates and
deletes. A correction is a new row: a later room entry for the same year, an opposite goal
adjustment, a new policy version.

- **Room** (P9, invariant 6): the figure as entered from CRA My Account, with an as-of date and a
  source note. The latest entry for a (kind, year) is the one that counts.
- **Room movements** (P9): contributions and withdrawals made outside Aurelius.
- **Goal adjustments** (P31): signed, non-zero, with a required note.
- **Policies**: versions are consecutive from 1; ``latest_policy`` is the one the engine uses.

Each insert runs in a savepoint, so a uniqueness failure raises ``Conflict`` and leaves the caller's
transaction usable. Balance entries and drift live with the plan loader, which they need.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Base, ContributionRoomRow, GoalAdjustmentRow, GoalRow, PolicyRow, RoomMovementRow
from app.integrations.json_decimal import assert_no_floats
from app.ledger.history import GoalAdjustment
from app.ledger.models import REGISTERED, AccountKind
from app.ledger.policy import AllocationPolicy
from app.ledger.room import MovementType, RoomEntry, RoomMovement
from app.services.ledger.clock import require_utc
from app.services.ledger.errors import Conflict, MissingConfig, NotFound


def _registered(kind: AccountKind) -> str:
    if kind not in REGISTERED:
        raise ValueError(f"contribution room applies to TFSA, FHSA and RRSP only, not {kind.value}")
    return kind.value


async def _insert(session: AsyncSession, row: Base, conflict: str) -> None:
    try:
        async with session.begin_nested():
            session.add(row)
    except IntegrityError:
        raise Conflict(conflict) from None


# --- Contribution room -------------------------------------------------------------------------


async def record_room(session: AsyncSession, entry: RoomEntry, *, now: datetime) -> RoomEntry:
    """Store a room figure. A second entry for the same (kind, year, as-of date) is a ``Conflict``."""
    require_utc(now)
    row = ContributionRoomRow(
        kind=_registered(entry.kind),
        year=entry.year,
        room=entry.room,
        as_of=entry.as_of,
        source_note=entry.source_note,
        created_at=now,
    )
    await _insert(session, row, f"{entry.kind.value} room for {entry.year} as of {entry.as_of} is already recorded")
    return entry


def _room(row: ContributionRoomRow) -> RoomEntry:
    return RoomEntry(
        kind=AccountKind(row.kind), year=row.year, room=row.room, as_of=row.as_of, source_note=row.source_note
    )


async def room_history(session: AsyncSession, kind: AccountKind, year: int) -> list[RoomEntry]:
    """Every entry for (kind, year), oldest as-of date first."""
    query = (
        select(ContributionRoomRow)
        .where(ContributionRoomRow.kind == _registered(kind), ContributionRoomRow.year == year)
        .order_by(ContributionRoomRow.as_of)
    )
    return [_room(row) for row in (await session.scalars(query)).all()]


async def latest_room(session: AsyncSession, kind: AccountKind, year: int) -> RoomEntry | None:
    """The entry with the latest as-of date, or ``None`` (stale room: the engine treats it as zero)."""
    history = await room_history(session, kind, year)
    return history[-1] if history else None


# --- Room movements ----------------------------------------------------------------------------


async def record_room_movement(
    session: AsyncSession, movement: RoomMovement, *, note: str | None = None, now: datetime
) -> RoomMovement:
    require_utc(now)
    row = RoomMovementRow(
        kind=_registered(movement.kind),
        on=movement.on,
        amount=movement.amount,
        movement=movement.movement.value,
        note=note or None,
        created_at=now,
    )
    await _insert(session, row, "room movement conflicts with an existing row")
    return movement


async def room_movements(session: AsyncSession, kind: AccountKind | None = None) -> list[RoomMovement]:
    """Manual movements, by date then insertion order; all registered kinds unless ``kind`` is given."""
    query = select(RoomMovementRow).order_by(RoomMovementRow.on, RoomMovementRow.id)
    if kind is not None:
        query = query.where(RoomMovementRow.kind == _registered(kind))
    return [
        RoomMovement(kind=AccountKind(r.kind), on=r.on, amount=r.amount, movement=MovementType(r.movement))
        for r in (await session.scalars(query)).all()
    ]


# --- Goal adjustments --------------------------------------------------------------------------


async def record_goal_adjustment(
    session: AsyncSession, adjustment: GoalAdjustment, *, note: str, now: datetime
) -> GoalAdjustment:
    """A signed correction to a goal's saved amount (P31). Zero and a blank note are refused."""
    require_utc(now)
    if adjustment.amount == 0:
        raise ValueError("a goal adjustment must be non-zero")
    if not note.strip():
        raise ValueError("a goal adjustment needs a note")
    if await session.get(GoalRow, adjustment.goal_id) is None:
        raise NotFound(f"no goal {adjustment.goal_id}")
    row = GoalAdjustmentRow(
        goal_id=adjustment.goal_id, amount=adjustment.amount, on=adjustment.on, note=note, created_at=now
    )
    await _insert(session, row, "goal adjustment conflicts with an existing row")
    return adjustment


async def goal_adjustments(session: AsyncSession, goal_id: str | None = None) -> list[GoalAdjustment]:
    query = select(GoalAdjustmentRow).order_by(GoalAdjustmentRow.on, GoalAdjustmentRow.id)
    if goal_id is not None:
        query = query.where(GoalAdjustmentRow.goal_id == goal_id)
    return [GoalAdjustment(goal_id=r.goal_id, amount=r.amount, on=r.on) for r in (await session.scalars(query)).all()]


# --- Policy versions ---------------------------------------------------------------------------


def _policy_document(policy: AllocationPolicy) -> dict[str, object]:
    # mode="json" writes Decimals as exact strings, so JSONB never holds a float.
    document: dict[str, object] = policy.model_dump(mode="json")
    assert_no_floats(document)
    return document


def _load_policy(document: object) -> AllocationPolicy:
    assert_no_floats(document)
    return AllocationPolicy.model_validate_json(json.dumps(document))


async def add_policy(session: AsyncSession, policy: AllocationPolicy, *, now: datetime) -> AllocationPolicy:
    """Store the next policy version. Versions run 1, 2, 3, ... with no gaps; anything else conflicts."""
    require_utc(now)
    current = await session.scalar(select(func.max(PolicyRow.version)))
    expected = (current or 0) + 1
    if policy.version != expected:
        raise Conflict(f"policy version must be {expected}, not {policy.version}")
    row = PolicyRow(version=policy.version, document=_policy_document(policy), created_at=now)
    await _insert(session, row, f"policy version {policy.version} already exists")
    return policy


async def get_policy(session: AsyncSession, version: int) -> AllocationPolicy:
    row = await session.get(PolicyRow, version)
    if row is None:
        raise NotFound(f"no policy version {version}")
    return _load_policy(row.document)


async def latest_policy(session: AsyncSession) -> AllocationPolicy:
    """The newest policy. ``MissingConfig`` if none has been stored yet."""
    row = await session.scalar(select(PolicyRow).order_by(PolicyRow.version.desc()).limit(1))
    if row is None:
        raise MissingConfig("no allocation policy has been stored")
    return _load_policy(row.document)


__all__ = [
    "add_policy",
    "get_policy",
    "goal_adjustments",
    "latest_policy",
    "latest_room",
    "record_goal_adjustment",
    "record_room",
    "record_room_movement",
    "room_history",
    "room_movements",
]
