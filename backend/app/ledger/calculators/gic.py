"""GIC ladder (P26).

The amount is split equally across ``rungs``: each rung gets ``floor_cents(amount / rungs)`` and rung 1
also takes the leftover cents, so the principals sum exactly to the amount. Rung ``k`` (1-based) is a
``k * term_years`` GIC bought on ``start`` at the rate entered for that term, compounding annually:

    value_k = principal_k * (1 + rate_k / 100) ^ (k * term_years)

Maturity is the same day and month ``k * term_years`` years later (Feb 29 falls on Feb 28).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from app.ledger.models import Frozen
from app.ledger.money import (
    CENT,
    HUNDRED,
    Percent,
    PositiveMoney,
    divide,
    floor_cents,
    half_up_cents,
    multiply,
    power,
)
from app.ledger.schedule import clamp_day

ONE = Decimal(1)


class TermRate(Frozen):
    term_years: int = Field(ge=1, le=100)
    #: Annual rate in percent for a GIC of this term. Required: no default.
    rate: Percent


class GicLadderInputs(Frozen):
    amount: PositiveMoney
    rungs: int = Field(ge=1, le=100)
    term_years: int = Field(ge=1, le=100)
    start: date
    #: One rate per term; must cover every ``k * term_years`` for k = 1..rungs.
    rates: tuple[TermRate, ...]

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.rungs * self.term_years > HUNDRED:
            raise ValueError("the longest rung must be at most 100 years")
        terms = [r.term_years for r in self.rates]
        if len(terms) != len(set(terms)):
            raise ValueError("rates has duplicate terms")
        missing = sorted({k * self.term_years for k in range(1, self.rungs + 1)} - set(terms))
        if missing:
            raise ValueError(f"no rate entered for term(s) {missing} years")
        if self.amount < multiply(CENT, Decimal(self.rungs)):
            raise ValueError("amount must be at least one cent per rung")
        return self


class Rung(Frozen):
    rung: int
    principal: Decimal
    term_years: int
    rate: Decimal
    maturity_date: date
    maturity_value: Decimal
    interest: Decimal


class GicLadderResult(Frozen):
    rungs: tuple[Rung, ...]
    total_principal: Decimal
    total_maturity_value: Decimal
    total_interest: Decimal


def gic_ladder(inputs: GicLadderInputs) -> GicLadderResult:
    """Equal-split ladder; each rung's value is rounded half-up to the cent (P21)."""
    rate_for = {r.term_years: r.rate for r in inputs.rates}
    base = floor_cents(divide(inputs.amount, Decimal(inputs.rungs)))
    first = inputs.amount - multiply(base, Decimal(inputs.rungs - 1))
    rungs = []
    for k in range(1, inputs.rungs + 1):
        principal = first if k == 1 else base
        term = k * inputs.term_years
        rate = rate_for[term]
        value = half_up_cents(multiply(principal, power(ONE + divide(rate, HUNDRED), term)))
        rungs.append(
            Rung(
                rung=k,
                principal=principal,
                term_years=term,
                rate=rate,
                maturity_date=clamp_day(inputs.start.year + term, inputs.start.month, inputs.start.day),
                maturity_value=value,
                interest=value - principal,
            )
        )
    total_value = sum((r.maturity_value for r in rungs), Decimal("0.00"))
    return GicLadderResult(
        rungs=tuple(rungs),
        total_principal=inputs.amount,
        total_maturity_value=total_value,
        total_interest=total_value - inputs.amount,
    )
