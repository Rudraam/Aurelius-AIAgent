"""Calculator invariants: no rate has a default (Phase 2 invariant 6), floats are rejected (invariant
8), the registry matches spec section 6, and results are deterministic.

The package-wide import, float and tax-literal scans in ``tests/unit/ledger/test_invariants.py`` also
cover ``app/ledger/calculators`` because it lives under ``app/ledger``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from pydantic.functional_validators import AfterValidator

from app.ledger import money
from app.ledger.calculators import CALCULATORS
from app.ledger.calculators.gic import TermRate
from app.ledger.models import IncomeSchedule, PayFrequency
from tests.unit.ledger.test_invariants import modules

RATE_VALIDATORS = {money._check_percent, money._check_positive_percent, money._check_return_rate}

SAMPLES: dict[str, dict[str, Any]] = {
    "compound-growth": {
        "starting_amount": Decimal("1000.00"),
        "contribution_per_period": Decimal("50.00"),
        "periods_per_year": 12,
        "years": 3,
        "annual_return": Decimal("5"),
    },
    "fee-drag": {
        "starting_amount": Decimal("1000.00"),
        "contribution_per_period": Decimal("50.00"),
        "periods_per_year": 12,
        "years": 3,
        "annual_return": Decimal("5"),
        "annual_fee": Decimal("1.5"),
    },
    "emergency-runway": {"fund_balance": Decimal("9000.00"), "monthly_essentials": Decimal("3000.00")},
    "goal-planner": {
        "target": Decimal("1000.00"),
        "current": Decimal("100.00"),
        "target_date": date(2027, 6, 30),
        "as_of": date(2027, 1, 2),
        "schedule": IncomeSchedule(frequency=PayFrequency.BIWEEKLY, anchor=date(2027, 1, 15)),
    },
    "gic-ladder": {
        "amount": Decimal("5000.00"),
        "rungs": 2,
        "term_years": 1,
        "start": date(2027, 1, 15),
        "rates": (TermRate(term_years=1, rate=Decimal("3")), TermRate(term_years=2, rate=Decimal("3.5"))),
    },
    "tfsa-vs-rrsp": {
        "contribution": Decimal("1000.00"),
        "years": 10,
        "annual_return": Decimal("5"),
        "marginal_rate_now": Decimal("30"),
        "marginal_rate_at_withdrawal": Decimal("20"),
    },
    "loan-amortization": {
        "principal": Decimal("10000.00"),
        "annual_rate": Decimal("7"),
        "compounding_per_year": 12,
        "payments_per_year": 12,
        "amortization_years": 2,
    },
    "fi-number": {"annual_expenses": Decimal("40000.00"), "withdrawal_rate": Decimal("4")},
}


def models() -> list[type[BaseModel]]:
    return [c.inputs for c in CALCULATORS.values()] + [TermRate]


def rate_fields(model: type[BaseModel]) -> list[str]:
    return [
        name
        for name, field in model.model_fields.items()
        if any(isinstance(m, AfterValidator) and m.func in RATE_VALIDATORS for m in field.metadata)
    ]


def test_registry_covers_the_spec_calculators() -> None:
    assert set(CALCULATORS) == {
        "compound-growth",
        "fee-drag",
        "emergency-runway",
        "goal-planner",
        "gic-ladder",
        "tfsa-vs-rrsp",
        "loan-amortization",
        "fi-number",
    }
    assert set(SAMPLES) == set(CALCULATORS)


def test_calculators_are_scanned_by_the_ledger_invariants() -> None:
    scanned = {p.name for p in modules() if "calculators" in p.parts}
    assert {"growth.py", "savings.py", "gic.py", "registered.py", "loan.py"} <= scanned


@pytest.mark.parametrize("model", models(), ids=lambda m: m.__name__)
def test_every_rate_is_required(model: type[BaseModel]) -> None:
    """Invariant 6: calculators take every rate as an input, with no default."""
    for name, field in model.model_fields.items():
        if name in rate_fields(model):
            assert field.is_required(), f"{model.__name__}.{name} has a default rate"


def test_rate_fields_are_found() -> None:
    found = {m.__name__: rate_fields(m) for m in models()}
    assert found["TfsaRrspInputs"] == ["annual_return", "marginal_rate_now", "marginal_rate_at_withdrawal"]
    assert found["LoanInputs"] == ["annual_rate"]
    assert found["FeeDragInputs"] == ["annual_return", "annual_fee"]
    assert found["FiNumberInputs"] == ["withdrawal_rate"]
    assert found["TermRate"] == ["rate"]


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_rejects_floats_for_every_decimal_input(name: str) -> None:
    calc = CALCULATORS[name]
    sample = SAMPLES[name]
    calc.compute(calc.inputs.model_validate(sample))  # the sample itself is valid
    decimals = [k for k, v in sample.items() if isinstance(v, Decimal)]
    assert decimals
    for key in decimals:
        with pytest.raises(ValidationError):
            calc.inputs.model_validate({**sample, key: float(sample[key])})


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_json_boundary_parses_strings_not_floats(name: str) -> None:
    calc = CALCULATORS[name]
    parsed = calc.inputs.model_validate(SAMPLES[name])
    assert calc.inputs.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_deterministic(name: str) -> None:
    calc = CALCULATORS[name]
    first = calc.compute(calc.inputs.model_validate(SAMPLES[name])).model_dump_json()
    second = calc.compute(calc.inputs.model_validate(SAMPLES[name])).model_dump_json()
    assert first == second
