"""Engine inputs: everything ``allocate`` needs, gathered by the service layer (invariant 2).

All models are frozen, strict and forbid extra fields. Derived figures that need history (projected
balances, sinking amounts already set aside, goal savings, remaining room) are computed by the pure
helpers in ``balances`` and ``room``, and the service passes in the results.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.ledger.money import ZERO, Money, NonNegativeMoney, PositiveMoney


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class PayFrequency(StrEnum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    SEMIMONTHLY = "semimonthly"
    MONTHLY = "monthly"


class IncomeSchedule(Frozen):
    """When pay arrives. Weekend and bank-holiday paydays move to the previous business day."""

    frequency: PayFrequency
    #: One nominal (unadjusted) payday. Weekly/biweekly step from it; monthly uses its day of month.
    anchor: date
    #: Semimonthly only (P13): two days of the month. 31 means the last day of the month.
    semimonthly_days: tuple[int, int] = (15, 31)

    @model_validator(mode="after")
    def _days(self) -> Self:
        first, second = self.semimonthly_days
        if not (1 <= first <= 28 and first < second <= 31):
            raise ValueError("semimonthly_days must satisfy 1 <= first <= 28 and first < second <= 31")
        return self


class BillFrequency(StrEnum):
    MONTHLY = "monthly"
    BIWEEKLY = "biweekly"
    ANNUAL = "annual"
    EVERY_N_MONTHS = "every_n_months"


class Bill(Frozen):
    id: str = Field(min_length=1)
    name: str
    amount: PositiveMoney
    frequency: BillFrequency
    #: The next due date. No occurrence before it is generated.
    anchor: date
    #: Month-based bills: the due day (31 = last day of shorter months). Defaults to anchor.day.
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    #: every_n_months only.
    interval_months: int | None = Field(default=None, ge=1)
    autopay: bool = False
    critical: bool = False
    #: Counts toward the emergency-fund target (P6).
    essential: bool = True
    #: Annual / every-n-months bills may be funded a little every pay (P7).
    sinking: bool = False
    #: Approved sinking lines since this bill was last due (computed by the service).
    sinking_funded: NonNegativeMoney = ZERO
    pay_from_account_id: str | None = None
    active: bool = True

    @model_validator(mode="after")
    def _shape(self) -> Self:
        month_based = self.frequency is not BillFrequency.BIWEEKLY
        if not month_based and self.day_of_month is not None:
            raise ValueError("biweekly bills have no day_of_month")
        if (self.frequency is BillFrequency.EVERY_N_MONTHS) != (self.interval_months is not None):
            raise ValueError("interval_months is required for, and only for, every_n_months bills")
        if self.sinking and self.frequency not in (BillFrequency.ANNUAL, BillFrequency.EVERY_N_MONTHS):
            raise ValueError("only annual and every_n_months bills can be sinking funds")
        return self

    @property
    def is_critical(self) -> bool:
        """P11: autopay bills and user-flagged bills are covered first in a shortfall."""
        return self.autopay or self.critical

    @property
    def due_day(self) -> int:
        return self.day_of_month if self.day_of_month is not None else self.anchor.day


class BudgetPeriod(StrEnum):
    PER_PAY = "per_pay"
    PER_MONTH = "per_month"


class BudgetLine(Frozen):
    id: str = Field(min_length=1)
    name: str
    amount: NonNegativeMoney
    period: BudgetPeriod
    account_id: str | None = None


class AccountKind(StrEnum):
    CHEQUING = "chequing"
    HISA = "hisa"
    GIC = "gic"
    TFSA = "tfsa"
    FHSA = "fhsa"
    RRSP = "rrsp"
    NON_REGISTERED = "non_registered"


REGISTERED: frozenset[AccountKind] = frozenset({AccountKind.TFSA, AccountKind.FHSA, AccountKind.RRSP})


class Account(Frozen):
    id: str = Field(min_length=1)
    kind: AccountKind
    currency: Literal["CAD", "USD"]
    #: e.g. "buffer", "emergency_fund".
    roles: tuple[str, ...] = ()
    #: Latest balance entry plus approved lines since (P8), computed by ``balances.project_balance``.
    projected_balance: Money

    @property
    def registered(self) -> bool:
        return self.kind in REGISTERED


class Goal(Frozen):
    id: str = Field(min_length=1)
    name: str
    target: PositiveMoney
    target_date: date | None = None
    account_id: str
    #: Lower number = higher priority.
    priority: int
    #: Saved toward the goal so far (computed by the service).
    saved: NonNegativeMoney
    active: bool = True


class RoomStatus(Frozen):
    """Remaining contribution room for one registered account type and calendar year (P9)."""

    kind: AccountKind
    year: int
    #: May be negative (over-contributed); the engine then allows nothing.
    remaining: Money

    @model_validator(mode="after")
    def _registered(self) -> Self:
        if self.kind not in REGISTERED:
            raise ValueError(f"contribution room applies to registered accounts only, not {self.kind}")
        return self


class LedgerInputs(Frozen):
    """One paycheque plus the state the waterfall needs."""

    payday: date
    net_pay: NonNegativeMoney
    expected_pay: NonNegativeMoney | None = None
    schedule: IncomeSchedule
    bank_holidays: tuple[date, ...] = ()
    bills: tuple[Bill, ...] = ()
    budget_lines: tuple[BudgetLine, ...] = ()
    accounts: tuple[Account, ...] = ()
    goals: tuple[Goal, ...] = ()
    room: tuple[RoomStatus, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> Self:
        for label, ids in (
            ("bill", [b.id for b in self.bills]),
            ("budget line", [b.id for b in self.budget_lines]),
            ("account", [a.id for a in self.accounts]),
            ("goal", [g.id for g in self.goals]),
            ("room", [f"{r.kind}:{r.year}" for r in self.room]),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} ids")
        return self
