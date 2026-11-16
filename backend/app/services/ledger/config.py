"""Editable ledger config (P29): income sources, accounts, bills, budget lines, goals, bank holidays.

Each kind has a frozen, strict ``*Spec`` model that is the whole row as the user states it. Shape rules
are the engine's own: a spec builds the matching ``app.ledger.models`` object while validating, so the
service can't accept a config the engine would refuse for its shape. Rules that need other rows (a
CAD destination account, roles) are left to the engine at plan time, which names the problem.

Slugs are immutable keys. ``replace`` rewrites every other field and stamps ``updated_at``; ``seeded``
writes also stamp ``seeded_at`` (P34). Rows are never deleted, because history references them: set
``active`` false instead. Bank holidays reference nothing, so they can be removed.

Every write runs in a savepoint inside the caller's transaction, so a uniqueness failure (a second
active income source, a duplicate slug) leaves the transaction usable and raises ``Conflict``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self, cast

from pydantic import Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import (
    SLUG,
    AccountRow,
    BankHolidayRow,
    Base,
    BillRow,
    BudgetLineRow,
    GoalRow,
    IncomeSourceRow,
)
from app.ledger.models import (
    AccountKind,
    Bill,
    BillFrequency,
    BudgetLine,
    BudgetPeriod,
    Frozen,
    IncomeSchedule,
    PayFrequency,
)
from app.ledger.money import ZERO, NonNegativeMoney, PositiveMoney
from app.services.ledger.clock import require_utc
from app.services.ledger.errors import Conflict, NotFound

Slug = Annotated[str, StringConstraints(pattern=SLUG, max_length=64)]


class IncomeSourceSpec(Frozen):
    id: Slug
    name: str = Field(min_length=1)
    frequency: PayFrequency
    anchor: date
    semimonthly_days: tuple[int, int] = (15, 31)
    expected_net: NonNegativeMoney | None = None
    active: bool = True

    @model_validator(mode="after")
    def _engine_shape(self) -> Self:
        self.schedule()
        return self

    def schedule(self) -> IncomeSchedule:
        return IncomeSchedule(frequency=self.frequency, anchor=self.anchor, semimonthly_days=self.semimonthly_days)


class AccountSpec(Frozen):
    id: Slug
    kind: AccountKind
    currency: Literal["CAD", "USD"]
    institution: str | None = None
    roles: tuple[str, ...] = ()
    active: bool = True

    @model_validator(mode="after")
    def _roles(self) -> Self:
        if len(set(self.roles)) != len(self.roles) or any(not r for r in self.roles):
            raise ValueError("roles must be distinct, non-empty strings")
        return self


class BillSpec(Frozen):
    id: Slug
    name: str = Field(min_length=1)
    category: str | None = None
    amount: PositiveMoney
    frequency: BillFrequency
    anchor: date
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    interval_months: int | None = Field(default=None, ge=1)
    autopay: bool = False
    critical: bool = False
    essential: bool = True
    sinking: bool = False
    pay_from_account_id: Slug | None = None
    active: bool = True

    @model_validator(mode="after")
    def _engine_shape(self) -> Self:
        self.to_bill(sinking_funded=ZERO)
        return self

    def to_bill(self, *, sinking_funded: Decimal) -> Bill:
        return Bill(
            **self.model_dump(exclude={"category"}),
            sinking_funded=sinking_funded,
        )


class BudgetLineSpec(Frozen):
    id: Slug
    name: str = Field(min_length=1)
    amount: NonNegativeMoney
    period: BudgetPeriod
    account_id: Slug | None = None
    active: bool = True

    def to_budget_line(self) -> BudgetLine:
        return BudgetLine(**self.model_dump(exclude={"active"}))


class GoalSpec(Frozen):
    id: Slug
    name: str = Field(min_length=1)
    target: PositiveMoney
    target_date: date | None = None
    account_id: Slug
    priority: int
    saved_before_aurelius: NonNegativeMoney = ZERO
    active: bool = True


class BankHolidaySpec(Frozen):
    day: date
    note: str | None = None


Spec = IncomeSourceSpec | AccountSpec | BillSpec | BudgetLineSpec | GoalSpec | BankHolidaySpec


@dataclass(frozen=True)
class _Kind:
    label: str
    row: type[Base]
    key: str
    to_columns: Callable[[Any], dict[str, Any]]
    from_row: Callable[[Any], dict[str, Any]]
    references: Callable[[Any], Sequence[str | None]]


def _plain(spec: Frozen) -> dict[str, Any]:
    return dict(spec)


def _columns_of(row: Any, names: Sequence[str]) -> dict[str, Any]:
    return {name: getattr(row, name) for name in names}


def _income_columns(spec: IncomeSourceSpec) -> dict[str, Any]:
    out = _plain(spec)
    first, second = out.pop("semimonthly_days")
    return {**out, "semimonthly_first": first, "semimonthly_second": second}


def _income_from_row(row: IncomeSourceRow) -> dict[str, Any]:
    out = _columns_of(row, ["id", "name", "frequency", "anchor", "expected_net", "active"])
    out["frequency"] = PayFrequency(row.frequency)
    out["semimonthly_days"] = (row.semimonthly_first, row.semimonthly_second)
    return out


def _account_columns(spec: AccountSpec) -> dict[str, Any]:
    return {**_plain(spec), "kind": spec.kind.value, "roles": list(spec.roles)}


def _account_from_row(row: AccountRow) -> dict[str, Any]:
    out = _columns_of(row, ["id", "currency", "institution", "active"])
    return {**out, "kind": AccountKind(row.kind), "roles": tuple(row.roles)}


def _bill_columns(spec: BillSpec) -> dict[str, Any]:
    return {**_plain(spec), "frequency": spec.frequency.value}


def _bill_from_row(row: BillRow) -> dict[str, Any]:
    names = [n for n in BillSpec.model_fields if n != "frequency"]
    return {**_columns_of(row, names), "frequency": BillFrequency(row.frequency)}


def _budget_columns(spec: BudgetLineSpec) -> dict[str, Any]:
    return {**_plain(spec), "period": spec.period.value}


def _budget_from_row(row: BudgetLineRow) -> dict[str, Any]:
    names = [n for n in BudgetLineSpec.model_fields if n != "period"]
    return {**_columns_of(row, names), "period": BudgetPeriod(row.period)}


def _fields(spec_type: type[Frozen]) -> Callable[[Any], dict[str, Any]]:
    return lambda row: _columns_of(row, list(spec_type.model_fields))


KINDS: dict[type[Frozen], _Kind] = {
    IncomeSourceSpec: _Kind("income source", IncomeSourceRow, "id", _income_columns, _income_from_row, lambda s: ()),
    AccountSpec: _Kind("account", AccountRow, "id", _account_columns, _account_from_row, lambda s: ()),
    BillSpec: _Kind("bill", BillRow, "id", _bill_columns, _bill_from_row, lambda s: (s.pay_from_account_id,)),
    BudgetLineSpec: _Kind(
        "budget line", BudgetLineRow, "id", _budget_columns, _budget_from_row, lambda s: (s.account_id,)
    ),
    GoalSpec: _Kind("goal", GoalRow, "id", _plain, _fields(GoalSpec), lambda s: (s.account_id,)),
    BankHolidaySpec: _Kind("bank holiday", BankHolidayRow, "day", _plain, _fields(BankHolidaySpec), lambda s: ()),
}


def _kind(spec_type: type[Frozen]) -> _Kind:
    try:
        return KINDS[spec_type]
    except KeyError:
        raise TypeError(f"not a config spec: {spec_type.__name__}") from None


def _to_spec[S: Spec](spec_type: type[S], row: Any) -> S:
    return cast("S", spec_type.model_validate(_kind(spec_type).from_row(row)))


async def _check_references(session: AsyncSession, kind: _Kind, spec: Spec) -> None:
    for account_id in kind.references(spec):
        if account_id is not None and await session.get(AccountRow, account_id) is None:
            raise NotFound(f"{kind.label} {getattr(spec, kind.key)}: no account {account_id!r}")


async def _flush(session: AsyncSession, kind: _Kind, key: object, apply: Callable[[], None]) -> None:
    try:
        async with session.begin_nested():
            apply()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
        detail = " (only one income source may be active, P32)" if constraint == "uq_income_source_one_active" else ""
        raise Conflict(f"{kind.label} {key}: conflicts with an existing row{detail}") from None


async def create[S: Spec](session: AsyncSession, spec: S, *, now: datetime, seeded: bool = False) -> S:
    """Add a new row. Raises ``Conflict`` if the key exists or a uniqueness rule fails."""
    require_utc(now)
    kind = _kind(type(spec))
    key = getattr(spec, kind.key)
    if await session.get(kind.row, key) is not None:
        raise Conflict(f"{kind.label} {key} already exists")
    await _check_references(session, kind, spec)
    row = kind.row(**kind.to_columns(spec), created_at=now, updated_at=now, seeded_at=now if seeded else None)
    await _flush(session, kind, key, lambda: session.add(row))
    return spec


async def replace[S: Spec](session: AsyncSession, spec: S, *, now: datetime, seeded: bool = False) -> S:
    """Rewrite an existing row (everything but its key). Raises ``NotFound`` if there is none."""
    require_utc(now)
    kind = _kind(type(spec))
    key = getattr(spec, kind.key)
    row: Any = await session.get(kind.row, key)
    if row is None:
        raise NotFound(f"no {kind.label} {key}")
    await _check_references(session, kind, spec)

    def apply() -> None:
        for name, value in kind.to_columns(spec).items():
            setattr(row, name, value)
        row.updated_at = now
        if seeded:
            row.seeded_at = now

    await _flush(session, kind, key, apply)
    return spec


async def get[S: Spec](session: AsyncSession, spec_type: type[S], key: object) -> S:
    kind = _kind(spec_type)
    row = await session.get(kind.row, key)
    if row is None:
        raise NotFound(f"no {kind.label} {key}")
    return _to_spec(spec_type, row)


async def list_all[S: Spec](session: AsyncSession, spec_type: type[S], *, active_only: bool = False) -> list[S]:
    """Every row of a kind, ordered by key. ``active_only`` applies to kinds with an ``active`` flag."""
    kind = _kind(spec_type)
    column = getattr(kind.row, kind.key)
    query = select(kind.row).order_by(column)
    if active_only and "active" in spec_type.model_fields:
        query = query.where(kind.row.active.is_(True))  # type: ignore[attr-defined]
    rows = (await session.scalars(query)).all()
    return [_to_spec(spec_type, row) for row in rows]


async def edited_since_seed[S: Spec](session: AsyncSession, spec_type: type[S], key: object) -> bool:
    """True if the row was changed after the seed last wrote it (or was never seeded) (P34)."""
    kind = _kind(spec_type)
    row: Any = await session.get(kind.row, key)
    if row is None:
        raise NotFound(f"no {kind.label} {key}")
    return bool(row.seeded_at is None or row.updated_at > row.seeded_at)


async def remove_bank_holiday(session: AsyncSession, day: date) -> None:
    row = await session.get(BankHolidayRow, day)
    if row is None:
        raise NotFound(f"no bank holiday {day.isoformat()}")
    await session.delete(row)
    await session.flush()
