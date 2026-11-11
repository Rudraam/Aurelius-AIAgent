"""Builders for fictional ledger data. Every number here is made up (invariant 9)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.ledger.models import (
    Account,
    AccountKind,
    Bill,
    BillFrequency,
    BudgetLine,
    BudgetPeriod,
    Goal,
    IncomeSchedule,
    LedgerInputs,
    PayFrequency,
    RoomStatus,
)
from app.ledger.policy import (
    AllocationPolicy,
    BufferPolicy,
    EmergencyFundPolicy,
    ExplorePolicy,
    FixedCap,
    GoalsPolicy,
    InvestingPolicy,
    PercentCap,
)


def D(value: str | int) -> Decimal:
    return Decimal(str(value))


def biweekly(anchor: date = date(2027, 1, 8)) -> IncomeSchedule:
    return IncomeSchedule(frequency=PayFrequency.BIWEEKLY, anchor=anchor)


def bill(id: str, amount: str, anchor: date, **kw: Any) -> Bill:
    kw.setdefault("frequency", BillFrequency.MONTHLY)
    return Bill(id=id, name=id.title(), amount=D(amount), anchor=anchor, **kw)


def account(id: str, kind: AccountKind, balance: str = "0.00", *roles: str, currency: str = "CAD") -> Account:
    return Account(id=id, kind=kind, currency=currency, roles=roles, projected_balance=D(balance))  # type: ignore[arg-type]


def standard_accounts() -> tuple[Account, ...]:
    return (
        account("chq", AccountKind.CHEQUING, "300.00", "buffer"),
        account("hisa", AccountKind.HISA, "1000.00", "emergency_fund"),
        account("tfsa", AccountKind.TFSA),
        account("fhsa", AccountKind.FHSA),
        account("nonreg", AccountKind.NON_REGISTERED),
        account("explore", AccountKind.NON_REGISTERED),
    )


def standard_inputs(**overrides: Any) -> LedgerInputs:
    """An ordinary biweekly paycheque on Friday 2027-01-08 (fictional)."""
    values: dict[str, Any] = {
        "payday": date(2027, 1, 8),
        "net_pay": D("2000.00"),
        "expected_pay": D("2000.00"),
        "schedule": biweekly(),
        "bills": (
            bill("rent", "900.00", date(2027, 1, 15), autopay=True),
            bill("phone", "45.00", date(2027, 1, 20)),
            bill("insurance", "600.00", date(2027, 6, 1), frequency=BillFrequency.ANNUAL, sinking=True),
        ),
        "budget_lines": (
            BudgetLine(id="groceries", name="Groceries", amount=D("400.00"), period=BudgetPeriod.PER_MONTH),
            BudgetLine(id="transit", name="Transit", amount=D("60.00"), period=BudgetPeriod.PER_PAY),
        ),
        "accounts": standard_accounts(),
        "goals": (
            Goal(
                id="trip",
                name="Trip",
                target=D("1200.00"),
                target_date=date(2027, 6, 30),
                account_id="hisa",
                priority=1,
                saved=D("0.00"),
            ),
        ),
        "room": (
            RoomStatus(kind=AccountKind.TFSA, year=2027, remaining=D("150.00")),
            RoomStatus(kind=AccountKind.FHSA, year=2027, remaining=D("1000.00")),
        ),
    }
    values.update(overrides)
    return LedgerInputs(**values)


def standard_policy(**overrides: Any) -> AllocationPolicy:
    values: dict[str, Any] = {
        "version": 1,
        "buffer": BufferPolicy(target=D("500.00")),
        "emergency_fund": EmergencyFundPolicy(months=D(3), per_pay_cap=PercentCap(kind="percent", percent=D(10))),
        "goals": GoalsPolicy(undated_per_pay=D("25.00")),
        "investing": InvestingPolicy(
            per_pay_limit=FixedCap(kind="fixed", amount=D("300.00")), targets=("tfsa", "fhsa", "nonreg")
        ),
        "explore": ExplorePolicy(percent=D(2), account_id="explore"),
    }
    values.update(overrides)
    return AllocationPolicy(**values)
