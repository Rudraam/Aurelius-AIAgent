"""TFSA vs RRSP comparison (P25).

One contribution of ``C`` **pre-tax** dollars, grown for ``Y`` years at ``r`` compounded annually:

    g    = (1 + r / 100) ^ Y
    TFSA = C * g * (1 - t_now / 100)          (the contribution is taxed first)
    RRSP = C * g * (1 - t_later / 100)        (the withdrawal is taxed at the end)

So the RRSP wins exactly when ``t_later < t_now``, and they are equal when the rates are equal. Both
are computed with the same factor order so that equal rates give identical results to the cent.
Tax rates are inputs: no rate, bracket or limit is in code (Phase 2 invariant 6).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.ledger.models import Frozen
from app.ledger.money import HUNDRED, Percent, PositiveMoney, ReturnRate, divide, half_up_cents, multiply, power

ONE = Decimal(1)


class TfsaRrspInputs(Frozen):
    #: Pre-tax dollars available to contribute.
    contribution: PositiveMoney
    years: int = Field(ge=1, le=100)
    annual_return: ReturnRate
    #: Marginal tax rate now, in percent. Required: no default.
    marginal_rate_now: Percent
    #: Expected marginal tax rate at withdrawal, in percent. Required: no default.
    marginal_rate_at_withdrawal: Percent


class TfsaRrspResult(Frozen):
    tfsa_contribution: Decimal
    tfsa_after_tax: Decimal
    rrsp_contribution: Decimal
    rrsp_before_tax: Decimal
    rrsp_after_tax: Decimal
    #: ``rrsp_after_tax - tfsa_after_tax``.
    difference: Decimal
    better: Literal["tfsa", "rrsp", "equal"]


def _after(base: Decimal, tax_percent: Decimal) -> Decimal:
    return multiply(base, ONE - divide(tax_percent, HUNDRED))


def tfsa_vs_rrsp(inputs: TfsaRrspInputs) -> TfsaRrspResult:
    """After-tax value of the same pre-tax contribution in a TFSA and in an RRSP (rounded half-up, P21)."""
    grown = multiply(inputs.contribution, power(ONE + divide(inputs.annual_return, HUNDRED), inputs.years))
    tfsa = half_up_cents(_after(grown, inputs.marginal_rate_now))
    rrsp = half_up_cents(_after(grown, inputs.marginal_rate_at_withdrawal))
    difference = rrsp - tfsa
    return TfsaRrspResult(
        tfsa_contribution=half_up_cents(_after(inputs.contribution, inputs.marginal_rate_now)),
        tfsa_after_tax=tfsa,
        rrsp_contribution=inputs.contribution,
        rrsp_before_tax=half_up_cents(grown),
        rrsp_after_tax=rrsp,
        difference=difference,
        better="rrsp" if difference > 0 else "tfsa" if difference < 0 else "equal",
    )
