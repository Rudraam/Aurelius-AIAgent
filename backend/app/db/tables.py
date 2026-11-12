"""The ledger schema (spec sections 3, 5 and 8; decisions P28-P35).

Config rows (income sources, accounts, bills, budget lines, goals, bank holidays) have a user-chosen,
immutable slug as their primary key, are edited in place, and carry ``updated_at`` and ``seeded_at``
so the seed can tell when a row was edited after it was last seeded (P34).

History rows (balance entries, drift, room entries and movements, goal adjustments, policy versions,
paycheques, plan lines and plan events) are append-only: a trigger installed by the migration rejects
UPDATE, DELETE and TRUNCATE. Plans may change only their ``state``, along the allowed transitions
(P30); a second trigger enforces that and rejects every other change.

Every CHECK and trigger here is created by ``migrations/versions/0001_ledger_core.py``; a test checks
that this metadata and the migrated database agree.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

MONEY = Numeric(18, 2, asdecimal=True)
SLUG = r"^[a-z0-9][a-z0-9-]*$"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False)


class ConfigRow:
    """Editable config: a slug primary key plus edit and seed timestamps."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: When the seed last wrote this row (None: never seeded). Edited after seeding if updated_at > seeded_at.
    seeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --- config ------------------------------------------------------------------------------------------


class IncomeSourceRow(ConfigRow, Base):
    __tablename__ = "income_source"
    __table_args__ = (
        CheckConstraint(f"id ~ '{SLUG}'", name="slug"),
        CheckConstraint("frequency IN ('weekly', 'biweekly', 'semimonthly', 'monthly')", name="frequency"),
        CheckConstraint(
            "semimonthly_first BETWEEN 1 AND 28 AND semimonthly_second > semimonthly_first"
            " AND semimonthly_second <= 31",
            name="semimonthly_days",
        ),
        CheckConstraint("expected_net IS NULL OR expected_net >= 0", name="expected_net"),
        # P32: at most one active income source (the service requires exactly one).
        Index("uq_income_source_one_active", text("(true)"), unique=True, postgresql_where=text("active")),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[str] = mapped_column(String(16), nullable=False)
    anchor: Mapped[date] = mapped_column(Date, nullable=False)
    semimonthly_first: Mapped[int] = mapped_column(Integer, nullable=False)
    semimonthly_second: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_net: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class AccountRow(ConfigRow, Base):
    __tablename__ = "account"
    __table_args__ = (
        CheckConstraint(f"id ~ '{SLUG}'", name="slug"),
        CheckConstraint("kind IN ('chequing', 'hisa', 'gic', 'tfsa', 'fhsa', 'rrsp', 'non_registered')", name="kind"),
        CheckConstraint("currency IN ('CAD', 'USD')", name="currency"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    institution: Mapped[str | None] = mapped_column(Text, nullable=True)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class BillRow(ConfigRow, Base):
    __tablename__ = "bill"
    __table_args__ = (
        CheckConstraint(f"id ~ '{SLUG}'", name="slug"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("frequency IN ('monthly', 'biweekly', 'annual', 'every_n_months')", name="frequency"),
        CheckConstraint("day_of_month IS NULL OR day_of_month BETWEEN 1 AND 31", name="day_of_month"),
        CheckConstraint("interval_months IS NULL OR interval_months >= 1", name="interval_months"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    frequency: Mapped[str] = mapped_column(String(16), nullable=False)
    anchor: Mapped[date] = mapped_column(Date, nullable=False)
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interval_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    autopay: Mapped[bool] = mapped_column(Boolean, nullable=False)
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False)
    essential: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sinking: Mapped[bool] = mapped_column(Boolean, nullable=False)
    pay_from_account_id: Mapped[str | None] = mapped_column(ForeignKey("account.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class BudgetLineRow(ConfigRow, Base):
    __tablename__ = "budget_line"
    __table_args__ = (
        CheckConstraint(f"id ~ '{SLUG}'", name="slug"),
        CheckConstraint("amount >= 0", name="amount"),
        CheckConstraint("period IN ('per_pay', 'per_month')", name="period"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("account.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class GoalRow(ConfigRow, Base):
    __tablename__ = "goal"
    __table_args__ = (
        CheckConstraint(f"id ~ '{SLUG}'", name="slug"),
        CheckConstraint("target > 0", name="target"),
        CheckConstraint("saved_before_aurelius >= 0", name="saved_before_aurelius"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    saved_before_aurelius: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class BankHolidayRow(ConfigRow, Base):
    __tablename__ = "bank_holiday"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# --- append-only history -----------------------------------------------------------------------------


class BalanceEntryRow(Base):
    __tablename__ = "balance_entry"
    __table_args__ = (
        UniqueConstraint("account_id", "as_of"),
        CheckConstraint("source IN ('manual', 'questrade_sync')", name="source"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = _ts()


class BalanceDriftRow(Base):
    __tablename__ = "balance_drift"
    __table_args__ = (CheckConstraint("difference = actual - projected", name="difference"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    balance_entry_id: Mapped[int] = mapped_column(ForeignKey("balance_entry.id"), nullable=False, unique=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    projected: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    actual: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    difference: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_at: Mapped[datetime] = _ts()


class ContributionRoomRow(Base):
    __tablename__ = "contribution_room"
    __table_args__ = (
        UniqueConstraint("kind", "year", "as_of"),
        CheckConstraint("kind IN ('tfsa', 'fhsa', 'rrsp')", name="kind"),
        CheckConstraint("length(source_note) > 0", name="source_note"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    room: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    source_note: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts()


class RoomMovementRow(Base):
    """Manual contributions and withdrawals made outside Aurelius (P9)."""

    __tablename__ = "room_movement"
    __table_args__ = (
        CheckConstraint("kind IN ('tfsa', 'fhsa', 'rrsp')", name="kind"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("movement IN ('contribution', 'withdrawal')", name="movement"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    on: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    movement: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()


class GoalAdjustmentRow(Base):
    __tablename__ = "goal_adjustment"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="amount"),
        CheckConstraint("length(note) > 0", name="note"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goal.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    on: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts()


class PolicyRow(Base):
    __tablename__ = "allocation_policy"
    __table_args__ = (CheckConstraint("version >= 1", name="version"),)

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _ts()


class PaychequeRow(Base):
    __tablename__ = "paycheque"
    __table_args__ = (CheckConstraint("net_pay >= 0", name="net_pay"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    income_source_id: Mapped[str] = mapped_column(ForeignKey("income_source.id"), nullable=False)
    payday: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    #: As first recorded. A void-and-replace corrects the net pay on the new plan, not here.
    net_pay: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_at: Mapped[datetime] = _ts()


class PlanRow(Base):
    __tablename__ = "allocation_plan"
    __table_args__ = (
        CheckConstraint("state IN ('proposed', 'approved', 'revised', 'voided')", name="state"),
        CheckConstraint("status IN ('ok', 'shortfall')", name="status"),
        CheckConstraint("net_pay >= 0", name="net_pay"),
        CheckConstraint("inputs_hash ~ '^[0-9a-f]{64}$'", name="inputs_hash"),
        # At most one live (proposed or approved) plan per paycheque.
        Index(
            "uq_allocation_plan_live_per_paycheque",
            "paycheque_id",
            unique=True,
            postgresql_where=text("state IN ('proposed', 'approved')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    paycheque_id: Mapped[int] = mapped_column(ForeignKey("paycheque.id"), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The engine's plan status (ok / shortfall).
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version: Mapped[int] = mapped_column(ForeignKey("allocation_policy.version"), nullable=False)
    inputs_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Canonical input snapshot bytes (inputs + policy), so the plan can be recomputed bit for bit.
    snapshot: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: Canonical plan bytes as the engine produced them.
    plan_json: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    payday: Mapped[date] = mapped_column(Date, nullable=False)
    net_pay: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    #: A revision's previous version.
    parent_plan_id: Mapped[int | None] = mapped_column(ForeignKey("allocation_plan.id"), nullable=True)
    #: A void-and-replace's voided plan.
    replaces_plan_id: Mapped[int | None] = mapped_column(ForeignKey("allocation_plan.id"), nullable=True)
    #: A revision's overrides, {line_id: "amount"}.
    overrides: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _ts()


class PlanLineRow(Base):
    __tablename__ = "plan_line"
    __table_args__ = (CheckConstraint("amount >= 0", name="amount"),)

    plan_id: Mapped[int] = mapped_column(ForeignKey("allocation_plan.id"), primary_key=True)
    line_id: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    bucket: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("account.id"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    reason_params: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    overridden_from: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)


class PlanEventRow(Base):
    __tablename__ = "plan_event"
    __table_args__ = (CheckConstraint("kind IN ('proposed', 'revised', 'approved', 'voided')", name="kind"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("allocation_plan.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    at: Mapped[datetime] = _ts()
    detail: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)


#: Tables whose rows can never change or disappear (a trigger rejects UPDATE, DELETE and TRUNCATE).
APPEND_ONLY = (
    "balance_entry",
    "balance_drift",
    "contribution_room",
    "room_movement",
    "goal_adjustment",
    "allocation_policy",
    "paycheque",
    "plan_line",
    "plan_event",
)

#: Allowed plan state transitions (P30). Everything else, and any other change to a plan, is rejected.
PLAN_TRANSITIONS = (
    ("proposed", "approved"),
    ("proposed", "revised"),
    ("proposed", "voided"),
    ("approved", "voided"),
)

CONFIG_TABLES = (IncomeSourceRow, AccountRow, BillRow, BudgetLineRow, GoalRow, BankHolidayRow)
