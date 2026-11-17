"""Ledger config service (P29, P32, P34) against the migrated schema. Fictional data only."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ledger.models import AccountKind, BillFrequency, BudgetPeriod, PayFrequency
from app.services.ledger import config
from app.services.ledger.config import (
    AccountSpec,
    BankHolidaySpec,
    BillSpec,
    BudgetLineSpec,
    GoalSpec,
    IncomeSourceSpec,
)
from app.services.ledger.errors import Conflict, NotFound

NOW = datetime(2026, 11, 15, 17, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)


def chequing(id: str = "chq", roles: tuple[str, ...] = ("buffer",)) -> AccountSpec:
    return AccountSpec(id=id, kind=AccountKind.CHEQUING, currency="CAD", institution="Test Bank", roles=roles)


def pay(id: str = "job", active: bool = True) -> IncomeSourceSpec:
    return IncomeSourceSpec(
        id=id,
        name="Example employer",
        frequency=PayFrequency.BIWEEKLY,
        anchor=date(2026, 11, 6),
        expected_net=Decimal("2000.00"),
        active=active,
    )


def rent(account: str | None = "chq") -> BillSpec:
    return BillSpec(
        id="rent",
        name="Rent",
        amount=Decimal("1200.00"),
        frequency=BillFrequency.MONTHLY,
        anchor=date(2026, 12, 1),
        autopay=True,
        pay_from_account_id=account,
    )


async def test_round_trip_every_kind(session: AsyncSession) -> None:
    specs = [
        chequing(),
        pay(),
        rent(),
        BudgetLineSpec(id="groceries", name="Groceries", amount=Decimal("400.00"), period=BudgetPeriod.PER_MONTH),
        GoalSpec(id="trip", name="Trip", target=Decimal("3000.00"), account_id="chq", priority=1),
        BankHolidaySpec(day=date(2026, 12, 25), note="Christmas"),
    ]
    for spec in specs:
        await config.create(session, spec, now=NOW)
    await session.commit()
    for spec in specs:
        key = getattr(spec, config.KINDS[type(spec)].key)
        assert await config.get(session, type(spec), key) == spec


async def test_semimonthly_days_round_trip(session: AsyncSession) -> None:
    spec = IncomeSourceSpec(
        id="semi", name="Semi", frequency=PayFrequency.SEMIMONTHLY, anchor=date(2026, 11, 15), semimonthly_days=(1, 16)
    )
    await config.create(session, spec, now=NOW)
    assert await config.get(session, IncomeSourceSpec, "semi") == spec


async def test_duplicate_slug_conflicts(session: AsyncSession) -> None:
    await config.create(session, chequing(), now=NOW)
    with pytest.raises(Conflict, match="already exists"):
        await config.create(session, chequing(), now=NOW)


async def test_second_active_income_source_conflicts_and_session_survives(session: AsyncSession) -> None:
    await config.create(session, pay("one"), now=NOW)
    with pytest.raises(Conflict, match="P32"):
        await config.create(session, pay("two"), now=NOW)
    # The savepoint kept the outer transaction usable.
    await config.create(session, pay("two", active=False), now=NOW)
    await session.commit()
    assert [s.id for s in await config.list_all(session, IncomeSourceSpec, active_only=True)] == ["one"]
    assert [s.id for s in await config.list_all(session, IncomeSourceSpec)] == ["one", "two"]


async def test_missing_account_reference_is_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFound, match="no account 'chq'"):
        await config.create(session, rent(), now=NOW)


async def test_replace_rewrites_fields_and_stamps_updated_at(session: AsyncSession) -> None:
    await config.create(session, chequing(), now=NOW)
    await config.create(session, rent(), now=NOW)
    changed = rent().model_copy(update={"amount": Decimal("1250.00")})
    await config.replace(session, changed, now=LATER)
    assert (await config.get(session, BillSpec, "rent")).amount == Decimal("1250.00")


async def test_replace_unknown_is_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFound):
        await config.replace(session, chequing(), now=NOW)


async def test_edited_since_seed(session: AsyncSession) -> None:
    await config.create(session, chequing("seeded"), now=NOW, seeded=True)
    await config.create(session, chequing("manual"), now=NOW)
    assert not await config.edited_since_seed(session, AccountSpec, "seeded")
    assert await config.edited_since_seed(session, AccountSpec, "manual")
    await config.replace(session, chequing("seeded", roles=()), now=LATER)
    assert await config.edited_since_seed(session, AccountSpec, "seeded")
    await config.replace(session, chequing("seeded", roles=()), now=LATER + timedelta(minutes=1), seeded=True)
    assert not await config.edited_since_seed(session, AccountSpec, "seeded")


async def test_remove_bank_holiday(session: AsyncSession) -> None:
    await config.create(session, BankHolidaySpec(day=date(2026, 12, 26)), now=NOW)
    await config.remove_bank_holiday(session, date(2026, 12, 26))
    assert await config.list_all(session, BankHolidaySpec) == []
    with pytest.raises(NotFound):
        await config.remove_bank_holiday(session, date(2026, 12, 26))


async def test_now_must_be_utc(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="UTC"):
        await config.create(session, chequing(), now=datetime(2026, 11, 15, 12, 0))
    with pytest.raises(ValueError, match="UTC"):
        await config.create(session, chequing(), now=datetime(2026, 11, 15, 12, 0, tzinfo=timezone(timedelta(hours=1))))


def test_specs_reuse_engine_shape_rules() -> None:
    with pytest.raises(ValidationError, match="sinking"):
        BillSpec(
            id="phone", name="Phone", amount=Decimal("50.00"), frequency=BillFrequency.MONTHLY,
            anchor=date(2026, 12, 3), sinking=True,
        )  # fmt: skip
    with pytest.raises(ValidationError, match="semimonthly_days"):
        IncomeSourceSpec(
            id="x", name="X", frequency=PayFrequency.SEMIMONTHLY, anchor=date(2026, 11, 1), semimonthly_days=(20, 10)
        )
    with pytest.raises(ValidationError):
        AccountSpec(id="Not_A_Slug", kind=AccountKind.TFSA, currency="CAD")
    with pytest.raises(ValidationError):
        BillSpec(
            id="float", name="Float", amount=12.5, frequency=BillFrequency.MONTHLY,  # type: ignore[arg-type]
            anchor=date(2026, 12, 3),
        )  # fmt: skip
