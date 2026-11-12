"""Compound growth with regular contributions, and fee drag (P20, P24).

Convention (P20): nominal periodic rate ``i = r / n``, contribution at the **end** of each period
(ordinary annuity). With ``N = n * Y`` periods:

    B_0 = P;  B_k = B_{k-1} * (1 + i) + C
    FV  = P * (1 + i)^N + C * ((1 + i)^N - 1) / i        (i = 0:  FV = P + C * N)

Fee drag (P24) runs the same calculation at the net rate ``r - MER``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from app.ledger.calculators.errors import CalculatorError
from app.ledger.models import Frozen
from app.ledger.money import (
    HUNDRED,
    NonNegativeMoney,
    Percent,
    ReturnRate,
    divide,
    half_up_cents,
    ledger_context,
    multiply,
)

ONE = Decimal(1)


class CompoundGrowthInputs(Frozen):
    starting_amount: NonNegativeMoney
    contribution_per_period: NonNegativeMoney
    periods_per_year: int = Field(ge=1, le=52)
    years: int = Field(ge=1, le=100)
    #: Annual return in percent (nominal, compounded per period). Required: no default rate.
    annual_return: ReturnRate


class YearRow(Frozen):
    year: int
    balance: Decimal
    contributed: Decimal
    growth: Decimal


class CompoundGrowthResult(Frozen):
    years: tuple[YearRow, ...]
    ending_balance: Decimal
    total_contributed: Decimal
    total_growth: Decimal


def _periodic_rate(annual_percent: Decimal, periods_per_year: int) -> Decimal:
    return divide(annual_percent, multiply(HUNDRED, Decimal(periods_per_year)))


def _grow(starting: Decimal, contribution: Decimal, periods_per_year: int, years: int, rate: Decimal) -> list[Decimal]:
    """Full-precision balance at the end of each year (B_{n*y} for y = 1..years)."""
    i = _periodic_rate(rate, periods_per_year)
    balance = starting
    out = []
    ctx = ledger_context()
    for _ in range(years):
        for _ in range(periods_per_year):
            balance = ctx.add(ctx.multiply(balance, ctx.add(ONE, i)), contribution)
        out.append(balance)
    return out


def compound_growth(inputs: CompoundGrowthInputs) -> CompoundGrowthResult:
    """Year-by-year balance of ``P`` growing at ``r / n`` per period with ``C`` added each period end.

    ``contributed`` is the regular contributions to date (``C * n * y``, starting amount excluded) and
    ``growth = balance - P - contributed``, both from the rounded balance, so each row adds up exactly.
    """
    exact = _grow(
        inputs.starting_amount,
        inputs.contribution_per_period,
        inputs.periods_per_year,
        inputs.years,
        inputs.annual_return,
    )
    rows = []
    for year, value in enumerate(exact, start=1):
        balance = half_up_cents(value)
        contributed = multiply(inputs.contribution_per_period, Decimal(inputs.periods_per_year * year))
        rows.append(
            YearRow(
                year=year,
                balance=balance,
                contributed=contributed,
                growth=balance - inputs.starting_amount - contributed,
            )
        )
    last = rows[-1]
    return CompoundGrowthResult(
        years=tuple(rows),
        ending_balance=last.balance,
        total_contributed=last.contributed,
        total_growth=last.growth,
    )


class FeeDragInputs(CompoundGrowthInputs):
    #: Annual fee (MER) in percent. Required: no default.
    annual_fee: Percent

    @model_validator(mode="after")
    def _net_rate(self) -> Self:
        if self.annual_return - self.annual_fee <= -HUNDRED:
            raise ValueError("annual_return - annual_fee must be greater than -100")
        return self


class FeeDragResult(Frozen):
    net_return: Decimal
    ending_without_fee: Decimal
    ending_with_fee: Decimal
    total_contributed: Decimal
    cost_of_fees: Decimal


def fee_drag(inputs: FeeDragInputs) -> FeeDragResult:
    """Ending balance without and with the fee: growth at ``r`` versus at ``r - MER`` (P24).

    ``cost_of_fees = ending_without_fee - ending_with_fee`` (from the rounded balances).
    """
    net = inputs.annual_return - inputs.annual_fee
    common = inputs.model_dump(exclude={"annual_fee"})
    without = compound_growth(CompoundGrowthInputs.model_validate(common))
    with_fee = compound_growth(CompoundGrowthInputs.model_validate({**common, "annual_return": net}))
    if without.total_contributed != with_fee.total_contributed:  # pragma: no cover - same inputs
        raise CalculatorError("contributions differ between the two runs")
    return FeeDragResult(
        net_return=net,
        ending_without_fee=without.ending_balance,
        ending_with_fee=with_fee.ending_balance,
        total_contributed=without.total_contributed,
        cost_of_fees=without.ending_balance - with_fee.ending_balance,
    )
