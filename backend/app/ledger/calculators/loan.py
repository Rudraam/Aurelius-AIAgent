"""Loan / mortgage amortization, lender-style cents (P22).

With nominal annual rate ``r`` compounded ``m`` times a year and ``p`` payments a year over ``Y`` years
(``N = p * Y`` payments), the periodic rate is

    i = (1 + r / (100 * m)) ^ (m / p) - 1            (m = 2: a Canadian fixed-rate mortgage)

and the payment is the level annuity payment rounded **up** to the cent:

    payment = ceil_cents(P * i / (1 - (1 + i)^-N))   (i = 0:  ceil_cents(P / N))

Each row's interest is ``half_up_cents(balance * i)`` and principal is ``payment - interest``. The final
payment is ``balance + interest`` (it absorbs every rounding remainder), and it comes early if a
smaller payment clears the balance. So every row is in whole cents, the principal column sums
exactly to ``P``, and ``total_paid = P + total_interest``.

Interest only falls as the balance falls, so the first row is the worst case: if its rounded interest
is not below the payment (a tiny loan over many periods, e.g. 2.81 over 408 payments), the loan can't
amortize in whole cents and ``CalculatorError`` is raised.

**Amortization vs term.** In Canada the *amortization* is how long the loan takes to repay in full;
the *term* is the length of the contract, after which the loan renews. With the optional
``term_years`` (at most the amortization), the result also has a ``term`` summary: the first
``p * term_years`` rows of the same schedule, and the balance left at the end of the term (the renewal
balance; zero when the term equals the amortization or the loan is repaid early).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from app.ledger.calculators.errors import CalculatorError
from app.ledger.models import Frozen
from app.ledger.money import (
    HUNDRED,
    ZERO,
    Percent,
    PositiveMoney,
    ceil_cents,
    divide,
    half_up_cents,
    multiply,
    power,
)

ONE = Decimal(1)


class LoanInputs(Frozen):
    principal: PositiveMoney
    #: Nominal annual rate in percent. Required: no default.
    annual_rate: Percent
    #: Compounding periods per year (2 for a Canadian fixed mortgage, 12 for most loans).
    compounding_per_year: int = Field(ge=1, le=52)
    payments_per_year: int = Field(ge=1, le=52)
    #: Years to repay in full (the amortization period, not a mortgage "term").
    amortization_years: int = Field(ge=1, le=100)
    #: Optional contract term in years (at most the amortization): adds the renewal balance.
    term_years: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def _term_within_amortization(self) -> Self:
        if self.term_years is not None and self.term_years > self.amortization_years:
            raise ValueError("term_years must not exceed amortization_years")
        return self


class LoanRow(Frozen):
    number: int
    payment: Decimal
    interest: Decimal
    principal: Decimal
    balance: Decimal


class TermSummary(Frozen):
    term_years: int
    #: Payments made within the term (fewer if the loan is repaid before the term ends).
    payments: int
    principal_paid: Decimal
    interest_paid: Decimal
    #: Balance owing at the end of the term: the renewal balance.
    balance_at_end: Decimal


class LoanResult(Frozen):
    #: Effective rate per payment period, in percent, at full precision.
    periodic_rate: Decimal
    payment: Decimal
    number_of_payments: int
    schedule: tuple[LoanRow, ...]
    total_paid: Decimal
    total_interest: Decimal
    #: Present only when ``term_years`` was given.
    term: TermSummary | None = None


def _term_summary(inputs: LoanInputs, rows: tuple[LoanRow, ...]) -> TermSummary | None:
    if inputs.term_years is None:
        return None
    within = rows[: inputs.payments_per_year * inputs.term_years]
    principal_paid = sum((r.principal for r in within), ZERO)
    return TermSummary(
        term_years=inputs.term_years,
        payments=len(within),
        principal_paid=principal_paid,
        interest_paid=sum((r.interest for r in within), ZERO),
        balance_at_end=inputs.principal - principal_paid,
    )


def periodic_rate(annual_percent: Decimal, compounding: int, payments: int) -> Decimal:
    """``(1 + r / (100 m)) ^ (m / p) - 1`` as a fraction (not percent)."""
    nominal = divide(annual_percent, multiply(HUNDRED, Decimal(compounding)))
    return power(ONE + nominal, divide(Decimal(compounding), Decimal(payments))) - ONE


def loan_amortization(inputs: LoanInputs) -> LoanResult:
    """Level payment and full schedule; see the module docstring for the formula and cents rule."""
    n = inputs.payments_per_year * inputs.amortization_years
    i = periodic_rate(inputs.annual_rate, inputs.compounding_per_year, inputs.payments_per_year)
    if i == ZERO:
        payment = ceil_cents(divide(inputs.principal, Decimal(n)))
    else:
        payment = ceil_cents(divide(multiply(inputs.principal, i), ONE - power(ONE + i, -n)))
    first_interest = half_up_cents(multiply(inputs.principal, i))
    if n > 1 and first_interest >= payment:
        raise CalculatorError(
            f"the payment ({payment}) does not exceed the first period's interest ({first_interest}) in whole cents;"
            " the loan is too small for this many payments"
        )
    balance = inputs.principal
    rows: list[LoanRow] = []
    for number in range(1, n + 1):
        interest = half_up_cents(multiply(balance, i))
        if number == n or balance + interest <= payment:
            rows.append(
                LoanRow(number=number, payment=balance + interest, interest=interest, principal=balance, balance=ZERO)
            )
            break
        principal = payment - interest
        if principal <= ZERO:  # pragma: no cover - excluded by the first-row check above
            raise CalculatorError("the payment does not cover the interest at cent precision")
        balance -= principal
        rows.append(LoanRow(number=number, payment=payment, interest=interest, principal=principal, balance=balance))
    total_interest = sum((r.interest for r in rows), ZERO)
    schedule = tuple(rows)
    return LoanResult(
        periodic_rate=multiply(i, HUNDRED),
        payment=payment,
        number_of_payments=len(rows),
        schedule=schedule,
        total_paid=inputs.principal + total_interest,
        total_interest=total_interest,
        term=_term_summary(inputs, schedule),
    )
