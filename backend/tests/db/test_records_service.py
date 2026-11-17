"""Append-only ledger records (P9, P29, P31) against the migrated schema. Fictional data only."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ledger.history import GoalAdjustment
from app.ledger.models import AccountKind
from app.ledger.room import MovementType, RoomEntry, RoomMovement
from app.services.ledger import config, records
from app.services.ledger.config import AccountSpec, GoalSpec
from app.services.ledger.errors import Conflict, MissingConfig, NotFound
from tests.unit.ledger.factories import standard_policy

NOW = datetime(2026, 11, 15, 17, 0, tzinfo=UTC)


def tfsa_room(room: str, as_of: date) -> RoomEntry:
    return RoomEntry(kind=AccountKind.TFSA, year=2026, room=Decimal(room), as_of=as_of, source_note="CRA My Account")


async def test_room_latest_entry_wins(session: AsyncSession) -> None:
    assert await records.latest_room(session, AccountKind.TFSA, 2026) is None
    await records.record_room(session, tfsa_room("7000.00", date(2026, 1, 1)), now=NOW)
    await records.record_room(session, tfsa_room("6500.00", date(2026, 6, 1)), now=NOW)
    await session.commit()
    latest = await records.latest_room(session, AccountKind.TFSA, 2026)
    assert latest is not None and latest.room == Decimal("6500.00")
    assert len(await records.room_history(session, AccountKind.TFSA, 2026)) == 2
    assert await records.latest_room(session, AccountKind.FHSA, 2026) is None


async def test_duplicate_room_entry_conflicts_and_session_survives(session: AsyncSession) -> None:
    await records.record_room(session, tfsa_room("7000.00", date(2026, 1, 1)), now=NOW)
    with pytest.raises(Conflict, match="already recorded"):
        await records.record_room(session, tfsa_room("6000.00", date(2026, 1, 1)), now=NOW)
    await records.record_room(session, tfsa_room("6000.00", date(2026, 2, 1)), now=NOW)
    await session.commit()


async def test_room_refuses_unregistered_kinds(session: AsyncSession) -> None:
    entry = RoomEntry(kind=AccountKind.HISA, year=2026, room=Decimal("1.00"), as_of=date(2026, 1, 1), source_note="x")
    with pytest.raises(ValueError, match="TFSA, FHSA and RRSP"):
        await records.record_room(session, entry, now=NOW)


async def test_room_movements_round_trip(session: AsyncSession) -> None:
    out = RoomMovement(
        kind=AccountKind.TFSA, on=date(2026, 3, 2), amount=Decimal("100.00"), movement=MovementType.WITHDRAWAL
    )
    inn = RoomMovement(
        kind=AccountKind.RRSP, on=date(2026, 2, 1), amount=Decimal("250.00"), movement=MovementType.CONTRIBUTION
    )
    await records.record_room_movement(session, out, note="moved to chequing", now=NOW)
    await records.record_room_movement(session, inn, now=NOW)
    assert await records.room_movements(session) == [inn, out]
    assert await records.room_movements(session, AccountKind.TFSA) == [out]


async def test_goal_adjustments(session: AsyncSession) -> None:
    await config.create(session, AccountSpec(id="hisa", kind=AccountKind.HISA, currency="CAD"), now=NOW)
    await config.create(
        session, GoalSpec(id="trip", name="Trip", target=Decimal("3000.00"), account_id="hisa", priority=1), now=NOW
    )
    up = GoalAdjustment(goal_id="trip", amount=Decimal("120.00"), on=date(2026, 11, 1))
    down = GoalAdjustment(goal_id="trip", amount=Decimal("-20.00"), on=date(2026, 11, 2))
    await records.record_goal_adjustment(session, up, note="gift", now=NOW)
    await records.record_goal_adjustment(session, down, note="fee", now=NOW)
    assert await records.goal_adjustments(session, "trip") == [up, down]

    with pytest.raises(ValueError, match="non-zero"):
        await records.record_goal_adjustment(
            session, up.model_copy(update={"amount": Decimal("0.00")}), note="x", now=NOW
        )
    with pytest.raises(ValueError, match="note"):
        await records.record_goal_adjustment(session, up, note="  ", now=NOW)
    with pytest.raises(NotFound, match="no goal"):
        await records.record_goal_adjustment(session, up.model_copy(update={"goal_id": "nope"}), note="x", now=NOW)


async def test_policy_versions_are_consecutive_and_round_trip(session: AsyncSession) -> None:
    with pytest.raises(MissingConfig):
        await records.latest_policy(session)
    with pytest.raises(Conflict, match="must be 1"):
        await records.add_policy(session, standard_policy(version=2), now=NOW)
    first = standard_policy()
    await records.add_policy(session, first, now=NOW)
    second = standard_policy(version=2)
    await records.add_policy(session, second, now=NOW + timedelta(days=1))
    await session.commit()
    assert await records.get_policy(session, 1) == first
    assert await records.latest_policy(session) == second
    with pytest.raises(Conflict, match="must be 3"):
        await records.add_policy(session, standard_policy(version=2), now=NOW)
    with pytest.raises(NotFound):
        await records.get_policy(session, 9)


async def test_stored_policy_has_no_floats(session: AsyncSession) -> None:
    await records.add_policy(session, standard_policy(), now=NOW)
    await session.commit()
    numbers = await session.scalars(
        text(
            "SELECT v::text FROM allocation_policy, jsonb_path_query(document, 'strict $.**') AS v"
            " WHERE jsonb_typeof(v) = 'number'"
        )
    )
    # Only integers (the version) may be JSON numbers; every amount and percent is a string.
    assert all(value.lstrip("-").isdigit() for value in numbers.all())


async def test_records_are_append_only(session: AsyncSession) -> None:
    await records.record_room(session, tfsa_room("7000.00", date(2026, 1, 1)), now=NOW)
    await session.commit()
    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(text("UPDATE contribution_room SET room = 1"))
    await session.rollback()


def test_room_entry_refuses_floats() -> None:
    with pytest.raises(ValidationError):
        RoomEntry(kind=AccountKind.TFSA, year=2026, room=7000.0, as_of=date(2026, 1, 1), source_note="x")  # type: ignore[arg-type]
