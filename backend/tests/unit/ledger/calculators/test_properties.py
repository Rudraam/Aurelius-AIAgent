"""Property tests on the calculators. Inputs are random and fictional."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.ledger.calculators.errors import CalculatorError
from app.ledger.calculators.gic import GicLadderInputs, TermRate, gic_ladder
from app.ledger.calculators.growth import CompoundGrowthInputs, FeeDragInputs, compound_growth, fee_drag
from app.ledger.calculators.loan import LoanInputs, loan_amortization, periodic_rate
from app.ledger.calculators.registered import TfsaRrspInputs, tfsa_vs_rrsp
from app.ledger.calculators.savings import GoalPlannerInputs, goal_planner
from app.ledger.models import IncomeSchedule, PayFrequency
from app.ledger.money import CENT, ZERO, ceil_cents, divide, half_up_cents, ledger_context, multiply, power

PROFILE = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

money = st.integers(min_value=0, max_value=10_000_000).map(lambda c: Decimal(c).scaleb(-2))
positive_money = st.integers(min_value=1, max_value=10_000_000).map(lambda c: Decimal(c).scaleb(-2))
percent = st.integers(min_value=0, max_value=3_000).map(lambda p: Decimal(p).scaleb(-2))
tax = st.integers(min_value=0, max_value=10_000).map(lambda p: Decimal(p).scaleb(-2))
periods = st.sampled_from([1, 2, 4, 12, 24, 26, 52])


def is_cents(value: Decimal) -> bool:
    return value == value.quantize(CENT)


@PROFILE
@given(start=money, contribution=money, n=periods, years=st.integers(1, 40), rate=percent)
def test_growth_matches_closed_form(start: Decimal, contribution: Decimal, n: int, years: int, rate: Decimal) -> None:
    result = compound_growth(
        CompoundGrowthInputs(
            starting_amount=start,
            contribution_per_period=contribution,
            periods_per_year=n,
            years=years,
            annual_return=rate,
        )
    )
    ctx = ledger_context()
    i = ctx.divide(rate, Decimal(100 * n))
    total = n * years
    if i == ZERO:
        exact = start + contribution * total
    else:
        g = ctx.power(1 + i, total)
        exact = ctx.add(ctx.multiply(start, g), ctx.divide(ctx.multiply(contribution, g - 1), i))
    assert abs(result.ending_balance - half_up_cents(exact)) <= CENT
    assert result.ending_balance == start + result.total_contributed + result.total_growth
    assert all(is_cents(row.balance) for row in result.years)
    if rate > 0:
        balances = [row.balance for row in result.years]
        assert balances == sorted(balances)


@PROFILE
@given(start=positive_money, contribution=money, n=periods, years=st.integers(1, 40), rate=percent, fee=percent)
def test_fee_drag_never_helps(
    start: Decimal, contribution: Decimal, n: int, years: int, rate: Decimal, fee: Decimal
) -> None:
    result = fee_drag(
        FeeDragInputs(
            starting_amount=start,
            contribution_per_period=contribution,
            periods_per_year=n,
            years=years,
            annual_return=rate,
            annual_fee=fee,
        )
    )
    assert result.cost_of_fees >= 0
    assert result.cost_of_fees == result.ending_without_fee - result.ending_with_fee
    if fee == 0:
        assert result.cost_of_fees == 0


@PROFILE
@given(
    principal=positive_money,
    rate=percent,
    m=periods,
    p=periods,
    years=st.integers(1, 35),
)
def test_loan_schedule_is_exact(principal: Decimal, rate: Decimal, m: int, p: int, years: int) -> None:
    inputs = LoanInputs(
        principal=principal, annual_rate=rate, compounding_per_year=m, payments_per_year=p, amortization_years=years
    )
    try:
        result = loan_amortization(inputs)
    except CalculatorError:
        # Only a loan whose rounded payment can't beat its first period's rounded interest is refused.
        i = periodic_rate(rate, m, p)
        n = p * years
        exact_payment = divide(multiply(principal, i), 1 - power(1 + i, -n))
        assert n > 1 and half_up_cents(multiply(principal, i)) >= ceil_cents(exact_payment)
        return
    rows = result.schedule
    assert 1 <= len(rows) == result.number_of_payments <= p * years
    assert sum(r.principal for r in rows) == principal
    assert sum(r.payment for r in rows) == result.total_paid == principal + result.total_interest
    assert rows[-1].balance == ZERO
    for previous, row in zip((None, *rows), rows, strict=False):
        assert all(is_cents(v) for v in (row.payment, row.interest, row.principal, row.balance))
        assert row.interest >= 0 and row.principal > 0 and row.balance >= 0
        assert row.payment == row.interest + row.principal
        opening = principal if previous is None else previous.balance
        assert row.balance == opening - row.principal
    assert all(r.payment == result.payment for r in rows[:-1])


@PROFILE
@given(
    amount=positive_money,
    rungs=st.integers(1, 10),
    term=st.integers(1, 5),
    rate=percent,
    start=st.dates(min_value=date(2026, 1, 1), max_value=date(2040, 12, 31)),
)
def test_gic_principals_conserve_the_amount(amount: Decimal, rungs: int, term: int, rate: Decimal, start: date) -> None:
    if amount < CENT * rungs:
        return
    result = gic_ladder(
        GicLadderInputs(
            amount=amount,
            rungs=rungs,
            term_years=term,
            start=start,
            rates=tuple(TermRate(term_years=k * term, rate=rate) for k in range(1, rungs + 1)),
        )
    )
    assert sum(r.principal for r in result.rungs) == amount
    assert all(r.principal > 0 and r.maturity_value >= r.principal for r in result.rungs)
    assert max(r.principal for r in result.rungs) - min(r.principal for r in result.rungs) < CENT * rungs
    assert [r.maturity_date.year - start.year for r in result.rungs] == [k * term for k in range(1, rungs + 1)]


@PROFILE
@given(c=positive_money, years=st.integers(1, 40), r=percent, now=tax, later=tax)
def test_tfsa_rrsp_ordering_follows_the_tax_rates(
    c: Decimal, years: int, r: Decimal, now: Decimal, later: Decimal
) -> None:
    result = tfsa_vs_rrsp(
        TfsaRrspInputs(
            contribution=c, years=years, annual_return=r, marginal_rate_now=now, marginal_rate_at_withdrawal=later
        )
    )
    assert result.difference == result.rrsp_after_tax - result.tfsa_after_tax
    if now == later:
        assert result.better == "equal"
    elif later < now:
        assert result.better in ("rrsp", "equal")
    else:
        assert result.better in ("tfsa", "equal")


@PROFILE
@given(
    target=positive_money,
    current=money,
    frequency=st.sampled_from(list(PayFrequency)),
    anchor=st.dates(min_value=date(2026, 1, 1), max_value=date(2027, 12, 31)),
    as_of=st.dates(min_value=date(2026, 6, 1), max_value=date(2028, 6, 1)),
    horizon=st.integers(-30, 800),
)
def test_goal_per_pay_reaches_the_target_by_less_than_a_cent_a_payday(
    target: Decimal, current: Decimal, frequency: PayFrequency, anchor: date, as_of: date, horizon: int
) -> None:
    schedule = IncomeSchedule(frequency=frequency, anchor=anchor)
    result = goal_planner(
        GoalPlannerInputs(
            target=target, current=current, target_date=as_of + timedelta(days=horizon), as_of=as_of, schedule=schedule
        )
    )
    if result.status == "reached":
        assert current >= target
        return
    if result.status == "no_paydays_remaining":
        assert result.per_pay is None and result.paydays_remaining == 0
        return
    assert result.per_pay is not None
    n = Decimal(result.paydays_remaining)
    assert result.per_pay * n >= result.outstanding
    assert (result.per_pay - CENT) * n < result.outstanding
    assert result.per_pay == ceil_cents(divide(result.outstanding, n))
