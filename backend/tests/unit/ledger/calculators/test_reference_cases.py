"""Hand-checked reference cases for every calculator (spec section 6, decisions P20-P27).

Each calculator has a zero-rate case, a zero-contribution case where that applies, a one-period edge
case and at least one textbook example with a published answer. Where the published answer rounds
the payment to the nearest cent and Aurelius rounds it up (P22), the comment says so.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.ledger.calculators.errors import CalculatorError
from app.ledger.calculators.gic import GicLadderInputs, TermRate, gic_ladder
from app.ledger.calculators.growth import CompoundGrowthInputs, FeeDragInputs, compound_growth, fee_drag
from app.ledger.calculators.loan import LoanInputs, loan_amortization
from app.ledger.calculators.registered import TfsaRrspInputs, tfsa_vs_rrsp
from app.ledger.calculators.savings import (
    FiNumberInputs,
    GoalPlannerInputs,
    RunwayInputs,
    emergency_runway,
    fi_number,
    goal_planner,
)
from app.ledger.models import IncomeSchedule, PayFrequency
from tests.unit.ledger.factories import D

# -- compound growth (P20) -------------------------------------------------------------------------


def growth(start: str, contribution: str, n: int, years: int, rate: str) -> CompoundGrowthInputs:
    return CompoundGrowthInputs(
        starting_amount=D(start),
        contribution_per_period=D(contribution),
        periods_per_year=n,
        years=years,
        annual_return=D(rate),
    )


def test_growth_zero_rate_is_plain_sum() -> None:
    result = compound_growth(growth("1000.00", "50.00", 12, 2, "0"))
    assert [r.balance for r in result.years] == [D("1600.00"), D("2200.00")]
    assert result.total_contributed == D("1200.00")
    assert result.total_growth == D("0.00")


def test_growth_zero_contributions_textbook_lump_sum() -> None:
    # 10,000 at 5% compounded annually for 10 years: 10000 * 1.05^10 = 16,288.946...
    result = compound_growth(growth("10000.00", "0.00", 1, 10, "5"))
    assert result.ending_balance == D("16288.95")
    assert result.total_contributed == D("0.00")
    assert result.total_growth == D("6288.95")
    assert result.years[0].balance == D("10500.00")


def test_growth_one_period_contribution_at_end() -> None:
    # Ordinary annuity: the contribution goes in at the end, so it earns nothing in its own period.
    result = compound_growth(growth("1000.00", "100.00", 1, 1, "10"))
    assert result.ending_balance == D("1200.00")
    assert result.total_growth == D("100.00")


def test_growth_textbook_monthly_annuity() -> None:
    # 100/month at 6% nominal compounded monthly for 10 years: 100 * (1.005^120 - 1) / 0.005 = 16,387.93
    result = compound_growth(growth("0.00", "100.00", 12, 10, "6"))
    assert result.ending_balance == D("16387.93")
    assert result.total_contributed == D("12000.00")
    assert result.total_growth == D("4387.93")


def test_growth_negative_return() -> None:
    assert compound_growth(growth("1000.00", "0.00", 1, 2, "-10")).ending_balance == D("810.00")


def test_growth_rows_add_up() -> None:
    result = compound_growth(growth("2500.00", "75.25", 26, 5, "7.3"))
    for row in result.years:
        assert row.balance == D("2500.00") + row.contributed + row.growth
        assert row.contributed == D("75.25") * 26 * row.year


def test_growth_rejects_return_at_or_below_minus_100() -> None:
    with pytest.raises(ValidationError):
        growth("1.00", "0.00", 1, 1, "-100")


# -- fee drag (P24) --------------------------------------------------------------------------------


def drag(fee: str, **over: object) -> FeeDragInputs:
    base: dict[str, object] = {
        "starting_amount": D("10000.00"),
        "contribution_per_period": D("0.00"),
        "periods_per_year": 1,
        "years": 25,
        "annual_return": D("6"),
        "annual_fee": D(fee),
    }
    return FeeDragInputs.model_validate({**base, **over})


def test_fee_drag_textbook_two_percent_mer() -> None:
    # 10,000 for 25 years: 1.06^25 = 4.29187 vs 1.04^25 = 2.66584.
    result = fee_drag(drag("2"))
    assert result.net_return == D("4")
    assert result.ending_without_fee == D("42918.71")
    assert result.ending_with_fee == D("26658.36")
    assert result.cost_of_fees == D("16260.35")


def test_fee_drag_zero_fee_costs_nothing() -> None:
    result = fee_drag(drag("0"))
    assert result.cost_of_fees == D("0.00")
    assert result.ending_with_fee == result.ending_without_fee


def test_fee_drag_zero_rate_and_contributions_only() -> None:
    result = fee_drag(drag("1", starting_amount=D("0.00"), contribution_per_period=D("100.00"), annual_return=D("0")))
    # 0% - 1% = -1% net. 100 a year for 25 years at -1%: 100 * (0.99^25 - 1) / -0.01 = 2,221.786...
    assert result.ending_without_fee == D("2500.00")
    assert result.ending_with_fee == D("2221.79")
    assert result.total_contributed == D("2500.00")


def test_fee_drag_one_period() -> None:
    result = fee_drag(drag("1", years=1))
    assert (result.ending_without_fee, result.ending_with_fee, result.cost_of_fees) == (
        D("10600.00"),
        D("10500.00"),
        D("100.00"),
    )


# -- emergency-fund runway (P27) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("balance", "monthly", "months"),
    [
        ("15000.00", "4000.00", "3.75"),
        ("10000.00", "3000.00", "3.33"),
        ("5999.99", "3000.00", "1.99"),  # 1.99999...: rounded down, never up to 2.00
        ("0.00", "2500.00", "0.00"),
        ("2500.00", "2500.00", "1.00"),
    ],
)
def test_runway(balance: str, monthly: str, months: str) -> None:
    result = emergency_runway(RunwayInputs(fund_balance=D(balance), monthly_essentials=D(monthly)))
    assert result.months_covered == D(months)


def test_runway_rejects_zero_essentials() -> None:
    with pytest.raises(ValidationError):
        RunwayInputs(fund_balance=D("100.00"), monthly_essentials=D("0.00"))


# -- goal planner (P23) ----------------------------------------------------------------------------

BIWEEKLY = IncomeSchedule(frequency=PayFrequency.BIWEEKLY, anchor=date(2027, 1, 15))


def plan(
    target: str, current: str, target_date: date, as_of: date = date(2027, 1, 2), **over: object
) -> GoalPlannerInputs:
    base: dict[str, object] = {
        "target": D(target),
        "current": D(current),
        "target_date": target_date,
        "as_of": as_of,
        "schedule": BIWEEKLY,
    }
    return GoalPlannerInputs.model_validate({**base, **over})


def test_goal_counts_a_payday_on_the_target_date() -> None:
    # Paydays 01-15, 01-29, 02-12, 02-26: four, the last one on the target date.
    result = goal_planner(plan("1000.00", "0.00", date(2027, 2, 26)))
    assert (result.status, result.paydays_remaining, result.per_pay) == ("on_track", 4, D("250.00"))
    assert (result.first_payday, result.last_payday) == (date(2027, 1, 15), date(2027, 2, 26))


def test_goal_rounds_up_so_the_target_is_reached() -> None:
    result = goal_planner(plan("1000.00", "0.00", date(2027, 2, 12)))
    assert (result.paydays_remaining, result.per_pay) == (3, D("333.34"))  # 3 x 333.34 >= 1000.00


def test_goal_one_payday_takes_everything_outstanding() -> None:
    result = goal_planner(plan("1000.00", "400.00", date(2027, 1, 20)))
    assert (result.outstanding, result.paydays_remaining, result.per_pay) == (D("600.00"), 1, D("600.00"))


def test_goal_already_reached() -> None:
    result = goal_planner(plan("1000.00", "1200.00", date(2027, 6, 1)))
    assert (result.status, result.outstanding, result.per_pay) == ("reached", D("0.00"), D("0.00"))


@pytest.mark.parametrize("target_date", [date(2027, 1, 10), date(2026, 12, 1)])
def test_goal_with_no_payday_left(target_date: date) -> None:
    result = goal_planner(plan("1000.00", "0.00", target_date))
    assert (result.status, result.paydays_remaining, result.per_pay) == ("no_paydays_remaining", 0, None)


def test_goal_uses_adjusted_paydays() -> None:
    # Monthly on the 31st: 2027-07-31 is a Saturday, so it moves to Friday 07-30.
    monthly = IncomeSchedule(frequency=PayFrequency.MONTHLY, anchor=date(2027, 1, 31))
    result = goal_planner(plan("900.00", "0.00", date(2027, 7, 30), as_of=date(2027, 5, 1), schedule=monthly))
    assert (result.paydays_remaining, result.last_payday, result.per_pay) == (3, date(2027, 7, 30), D("300.00"))
    holiday = goal_planner(
        plan(
            "900.00",
            "0.00",
            date(2027, 5, 31),
            as_of=date(2027, 5, 1),
            schedule=monthly,
            bank_holidays=(date(2027, 5, 31),),
        )
    )
    assert holiday.last_payday == date(2027, 5, 28)


# -- GIC ladder (P26) ------------------------------------------------------------------------------


def ladder(
    amount: str, rungs: int, term: int, rates: dict[int, str], start: date = date(2027, 1, 15)
) -> GicLadderInputs:
    return GicLadderInputs(
        amount=D(amount),
        rungs=rungs,
        term_years=term,
        start=start,
        rates=tuple(TermRate(term_years=t, rate=D(r)) for t, r in rates.items()),
    )


def test_gic_textbook_half_up_rounding() -> None:
    # 1,000 at 5% compounded annually for 3 years = 1,157.625 exactly: half-up gives .63 (half-even .62).
    rung = gic_ladder(ladder("1000.00", 1, 3, {3: "5"})).rungs[0]
    assert (rung.maturity_value, rung.interest, rung.maturity_date) == (D("1157.63"), D("157.63"), date(2030, 1, 15))


def test_gic_zero_rate() -> None:
    result = gic_ladder(ladder("3000.00", 3, 1, {1: "0", 2: "0", 3: "0"}))
    assert [r.maturity_value for r in result.rungs] == [D("1000.00")] * 3
    assert result.total_interest == D("0.00")


def test_gic_one_rung_one_year() -> None:
    result = gic_ladder(ladder("1000.00", 1, 1, {1: "5"}))
    assert (result.total_maturity_value, result.total_interest) == (D("1050.00"), D("50.00"))


def test_gic_split_puts_leftover_cents_on_rung_one_and_dates_step_by_term() -> None:
    result = gic_ladder(ladder("10000.00", 3, 2, {2: "4", 4: "4.5", 6: "5"}, start=date(2028, 2, 29)))
    assert [r.principal for r in result.rungs] == [D("3333.34"), D("3333.33"), D("3333.33")]
    assert sum(r.principal for r in result.rungs) == result.total_principal == D("10000.00")
    assert [r.term_years for r in result.rungs] == [2, 4, 6]
    # Feb 29 start: 2030 and 2034 aren't leap years (Feb 28); 2032 is (Feb 29).
    assert [r.maturity_date for r in result.rungs] == [date(2030, 2, 28), date(2032, 2, 29), date(2034, 2, 28)]
    assert result.rungs[0].maturity_value == D("3605.34")  # 3333.34 * 1.04^2 = 3605.3405...
    assert result.total_interest == result.total_maturity_value - D("10000.00")


@pytest.mark.parametrize(
    ("rates", "match"),
    [
        ({1: "4"}, "no rate entered"),
        ({1: "4", 2: "4"}, None),
    ],
)
def test_gic_requires_a_rate_for_every_rung(rates: dict[int, str], match: str | None) -> None:
    if match is None:
        assert len(gic_ladder(ladder("100.00", 2, 1, rates)).rungs) == 2
        return
    with pytest.raises(ValidationError, match=match):
        ladder("100.00", 2, 1, rates)


def test_gic_rejects_duplicate_terms_and_sub_cent_rungs() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        GicLadderInputs(
            amount=D("100.00"),
            rungs=1,
            term_years=1,
            start=date(2027, 1, 1),
            rates=(TermRate(term_years=1, rate=D("4")), TermRate(term_years=1, rate=D("5"))),
        )
    with pytest.raises(ValidationError, match="one cent per rung"):
        ladder("0.02", 3, 1, {1: "1", 2: "1", 3: "1"})


# -- TFSA vs RRSP (P25) ----------------------------------------------------------------------------


def compare(c: str, years: int, r: str, now: str, later: str) -> TfsaRrspInputs:
    return TfsaRrspInputs(
        contribution=D(c),
        years=years,
        annual_return=D(r),
        marginal_rate_now=D(now),
        marginal_rate_at_withdrawal=D(later),
    )


def test_tfsa_rrsp_equal_rates_are_equivalent() -> None:
    # 1,000 pre-tax at 6% for 10 years (1.06^10 = 1.790848) taxed at 40% either way: 1,074.51.
    result = tfsa_vs_rrsp(compare("1000.00", 10, "6", "40", "40"))
    assert (result.tfsa_contribution, result.tfsa_after_tax) == (D("600.00"), D("1074.51"))
    assert (result.rrsp_before_tax, result.rrsp_after_tax) == (D("1790.85"), D("1074.51"))
    assert (result.difference, result.better) == (D("0.00"), "equal")


def test_tfsa_rrsp_textbook_lower_rate_in_retirement_favours_rrsp() -> None:
    result = tfsa_vs_rrsp(compare("1000.00", 10, "6", "40", "30"))
    assert (result.tfsa_after_tax, result.rrsp_after_tax) == (D("1074.51"), D("1253.59"))
    assert (result.difference, result.better) == (D("179.08"), "rrsp")


def test_tfsa_rrsp_zero_return() -> None:
    result = tfsa_vs_rrsp(compare("1000.00", 5, "0", "20", "40"))
    assert (result.tfsa_after_tax, result.rrsp_after_tax, result.better) == (D("800.00"), D("600.00"), "tfsa")


def test_tfsa_rrsp_one_year() -> None:
    result = tfsa_vs_rrsp(compare("1000.00", 1, "10", "30", "30"))
    assert result.tfsa_after_tax == result.rrsp_after_tax == D("770.00")


# -- loan / amortization (P22) ---------------------------------------------------------------------


def loan(principal: str, rate: str, m: int, p: int, years: int) -> LoanInputs:
    return LoanInputs(
        principal=D(principal),
        annual_rate=D(rate),
        compounding_per_year=m,
        payments_per_year=p,
        amortization_years=years,
    )


def test_loan_zero_rate_even_split() -> None:
    result = loan_amortization(loan("1200.00", "0", 12, 12, 1))
    assert (result.payment, result.number_of_payments, result.total_interest) == (D("100.00"), 12, D("0.00"))


def test_loan_zero_rate_last_payment_absorbs_the_remainder() -> None:
    result = loan_amortization(loan("1000.00", "0", 3, 3, 1))
    assert result.payment == D("333.34")
    assert [r.payment for r in result.schedule] == [D("333.34"), D("333.34"), D("333.32")]
    assert result.total_paid == D("1000.00")


def test_loan_one_period() -> None:
    result = loan_amortization(loan("1000.00", "12", 1, 1, 1))
    (row,) = result.schedule
    assert (row.payment, row.interest, row.principal, row.balance) == (
        D("1120.00"),
        D("120.00"),
        D("1000.00"),
        D("0.00"),
    )


def test_loan_textbook_one_year_monthly() -> None:
    # 1,000 at 12% compounded monthly over 12 payments: the annuity payment is 88.8488 (textbook 88.85).
    result = loan_amortization(loan("1000.00", "12", 12, 12, 1))
    assert result.payment == D("88.85")
    assert result.schedule[0].interest == D("10.00")
    assert result.schedule[-1].payment == D("88.84")
    assert result.total_interest == D("66.19")


def test_loan_textbook_canadian_mortgage_semiannual_compounding() -> None:
    # 100,000 at 5% compounded semi-annually, monthly payments, 25 years.
    # Periodic rate (1.025)^(1/6) - 1 = 0.4123915%. The exact payment is 581.6045..., which textbooks
    # round to 581.60; lender-style rounding (P22) rounds it up to 581.61.
    result = loan_amortization(loan("100000.00", "5", 2, 12, 25))
    assert str(result.periodic_rate).startswith("0.41239154651")
    assert result.payment == D("581.61")
    assert result.number_of_payments == 300
    assert result.schedule[0].interest == D("412.39")


def test_loan_textbook_us_mortgage_monthly_compounding() -> None:
    # 200,000 at 6% compounded monthly over 30 years: exact payment 1,199.1010 (textbook 1,199.10),
    # rounded up to 1,199.11 (P22). The final payment absorbs the overpayment.
    result = loan_amortization(loan("200000.00", "6", 12, 12, 30))
    assert result.payment == D("1199.11")
    assert result.number_of_payments == 360
    assert result.schedule[0].interest == D("1000.00")
    assert result.schedule[-1].payment < result.payment


def test_loan_too_small_to_amortize_in_cents_is_refused() -> None:
    # Found by hypothesis: 2.81 over 408 payments rounds to a 0.01 payment, and the first period's
    # interest (0.005...) rounds half-up to 0.01 as well, so no principal would ever be repaid.
    with pytest.raises(CalculatorError, match="too small"):
        loan_amortization(loan("2.81", "4.27", 52, 24, 17))


# -- financial-independence number -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("expenses", "rate", "target"),
    [
        ("40000.00", "4", "1000000.00"),
        ("30000.00", "3.5", "857142.86"),
        ("50000.00", "100", "50000.00"),
    ],
)
def test_fi_number(expenses: str, rate: str, target: str) -> None:
    assert fi_number(FiNumberInputs(annual_expenses=D(expenses), withdrawal_rate=D(rate))).target_portfolio == D(target)


def test_fi_number_rejects_zero_withdrawal_rate() -> None:
    with pytest.raises(ValidationError):
        FiNumberInputs(annual_expenses=D("40000.00"), withdrawal_rate=D("0"))
