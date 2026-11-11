"""Emergency-fund runway, goal planner and financial-independence number."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from pydantic import Field

from app.ledger.models import Frozen, IncomeSchedule
from app.ledger.money import (
    CENT,
    HUNDRED,
    ZERO,
    NonNegativeMoney,
    PositiveMoney,
    PositivePercent,
    ceil_cents,
    divide,
    half_up_cents,
    ledger_context,
    multiply,
)
from app.ledger.schedule import paydays

# -- emergency-fund runway ------------------------------------------------------------------------


class RunwayInputs(Frozen):
    fund_balance: NonNegativeMoney
    monthly_essentials: PositiveMoney


class RunwayResult(Frozen):
    months_covered: Decimal


def emergency_runway(inputs: RunwayInputs) -> RunwayResult:
    """``months = balance / monthly essentials``, rounded **down** to 0.01 months (P27).

    Rounding down never overstates how long the fund lasts.
    """
    months = divide(inputs.fund_balance, inputs.monthly_essentials)
    return RunwayResult(months_covered=months.quantize(CENT, rounding=ROUND_FLOOR, context=ledger_context()))


# -- goal planner ---------------------------------------------------------------------------------


class GoalPlannerInputs(Frozen):
    target: PositiveMoney
    current: NonNegativeMoney
    target_date: date
    #: The day the plan is made (the service passes the Toronto date; the calculator reads no clock).
    as_of: date
    schedule: IncomeSchedule
    #: User-entered bank holidays (paydays on them move to the previous business day).
    bank_holidays: tuple[date, ...] = Field(default=())


GoalStatus = Literal["reached", "on_track", "no_paydays_remaining"]


class GoalPlannerResult(Frozen):
    status: GoalStatus
    outstanding: Decimal
    paydays_remaining: int
    #: Required per pay, rounded **up** to the cent (P23). None when no payday is left.
    per_pay: Decimal | None
    first_payday: date | None
    last_payday: date | None


def goal_planner(inputs: GoalPlannerInputs) -> GoalPlannerResult:
    """``per_pay = ceil_cents(outstanding / paydays)`` over the adjusted paydays in ``[as_of, target_date]``.

    Paydays on the target date count, as in the engine's goal rule. Rounding up (P23) means saving
    ``per_pay`` every pay reaches the target, overshooting by less than one cent per payday.
    """
    outstanding = max(inputs.target - inputs.current, ZERO)
    days = (
        paydays(inputs.schedule, inputs.bank_holidays, inputs.as_of, inputs.target_date + timedelta(days=1))
        if inputs.target_date >= inputs.as_of
        else []
    )
    first, last = (days[0], days[-1]) if days else (None, None)
    if outstanding == ZERO:
        return GoalPlannerResult(
            status="reached",
            outstanding=ZERO,
            paydays_remaining=len(days),
            per_pay=ZERO,
            first_payday=first,
            last_payday=last,
        )
    if not days:
        return GoalPlannerResult(
            status="no_paydays_remaining",
            outstanding=outstanding,
            paydays_remaining=0,
            per_pay=None,
            first_payday=None,
            last_payday=None,
        )
    return GoalPlannerResult(
        status="on_track",
        outstanding=outstanding,
        paydays_remaining=len(days),
        per_pay=ceil_cents(divide(outstanding, Decimal(len(days)))),
        first_payday=first,
        last_payday=last,
    )


# -- financial-independence number ----------------------------------------------------------------


class FiNumberInputs(Frozen):
    annual_expenses: PositiveMoney
    #: Sustainable withdrawal rate in percent. Required: no default.
    withdrawal_rate: PositivePercent


class FiNumberResult(Frozen):
    target_portfolio: Decimal


def fi_number(inputs: FiNumberInputs) -> FiNumberResult:
    """``target = annual expenses / (withdrawal rate / 100)``, rounded half-up to the cent (P21)."""
    return FiNumberResult(
        target_portfolio=half_up_cents(divide(multiply(inputs.annual_expenses, HUNDRED), inputs.withdrawal_rate))
    )
