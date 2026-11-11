"""The engine's output: an allocation plan with structured reasons (invariant 3).

A plan is frozen. Every line and notice carries a reason code with string parameters, which Phase 4
turns into prose. Lines are kept even at zero, so every item considered is visible and can be
overridden in a revision.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from app.ledger.models import Frozen
from app.ledger.money import Money, NonNegativeMoney


class Bucket(StrEnum):
    BILLS = "bills"
    ESSENTIALS = "essentials"
    BUFFER = "buffer"
    EMERGENCY_FUND = "emergency_fund"
    GOALS = "goals"
    INVESTING = "investing"
    EXPLORE = "explore"
    SPENDING = "spending"
    SURPLUS = "surplus"


class PlanStatus(StrEnum):
    OK = "ok"
    SHORTFALL = "shortfall"


Params = tuple[tuple[str, str], ...]


def params(values: Mapping[str, object]) -> Params:
    """Canonical parameters: sorted keys, values as strings (dates ISO, Decimals exact)."""
    out: list[tuple[str, str]] = []
    for key in sorted(values):
        value = values[key]
        if isinstance(value, float):
            raise TypeError(f"float parameter {key!r}")
        if isinstance(value, date):
            out.append((key, value.isoformat()))
        elif isinstance(value, Decimal):
            out.append((key, str(value)))
        else:
            out.append((key, str(value)))
    return tuple(out)


class Reason(Frozen):
    code: str
    params: Params = ()


class Line(Frozen):
    #: Stable within a plan, used for overrides: e.g. "bill:rent:2027-01-01", "goal:car", "spending".
    line_id: str = Field(min_length=1)
    bucket: Bucket
    account_id: str | None = None
    amount: NonNegativeMoney
    reason: Reason
    #: Set on a revision: the amount before the override.
    overridden_from: NonNegativeMoney | None = None


class Notice(Frozen):
    code: str
    params: Params = ()


SPENDING_LINE_ID = "spending"


class Plan(Frozen):
    policy_version: int
    #: sha256 of the canonical input snapshot (inputs + policy).
    inputs_hash: str
    payday: date
    next_payday: date
    net_pay: NonNegativeMoney
    status: PlanStatus
    lines: tuple[Line, ...]
    notices: tuple[Notice, ...] = ()

    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0.00"))

    def by_bucket(self) -> dict[Bucket, Decimal]:
        out: dict[Bucket, Decimal] = {}
        for line in self.lines:
            out[line.bucket] = out.get(line.bucket, Decimal("0.00")) + line.amount
        return out

    def line(self, line_id: str) -> Line:
        for candidate in self.lines:
            if candidate.line_id == line_id:
                return candidate
        raise KeyError(line_id)


__all__ = ["SPENDING_LINE_ID", "Bucket", "Line", "Money", "Notice", "Plan", "PlanStatus", "Reason", "params"]
