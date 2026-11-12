"""Phase 2 calculators (M2): pure functions from a strict input model to a result model.

Same rules as the rest of ``app/ledger`` (pure, Decimal only, no tax law in code):

- every rate is a **required** input in percent; no rate has a default (Phase 2 invariant 6);
- inputs are frozen, strict Pydantic models, so floats are rejected at the boundary;
- arithmetic runs at full precision under ``ledger_context``; results are rounded to the cent only at
  output: half-up for projections (P21), up for required savings and loan payments (P22, P23), down
  for runway months (P27).

``CALCULATORS`` maps each endpoint name (``POST /calculators/<name>``, built in M4) to its input model
and function.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.ledger.calculators.errors import CalculatorError
from app.ledger.calculators.gic import GicLadderInputs, gic_ladder
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


@dataclass(frozen=True)
class Calculator:
    inputs: type[BaseModel]
    compute: Callable[[Any], BaseModel]


CALCULATORS: dict[str, Calculator] = {
    "compound-growth": Calculator(CompoundGrowthInputs, compound_growth),
    "fee-drag": Calculator(FeeDragInputs, fee_drag),
    "emergency-runway": Calculator(RunwayInputs, emergency_runway),
    "goal-planner": Calculator(GoalPlannerInputs, goal_planner),
    "gic-ladder": Calculator(GicLadderInputs, gic_ladder),
    "tfsa-vs-rrsp": Calculator(TfsaRrspInputs, tfsa_vs_rrsp),
    "loan-amortization": Calculator(LoanInputs, loan_amortization),
    "fi-number": Calculator(FiNumberInputs, fi_number),
}

__all__ = ["CALCULATORS", "Calculator", "CalculatorError"]
