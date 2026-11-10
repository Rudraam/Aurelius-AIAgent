"""The allocation policy: a versioned document validated by Pydantic (spec section 3, "Allocation policy").

Bills (bucket 1) and essentials (bucket 2) always come first, and spending always comes last. The
policy orders the five middle buckets and sets every target, cap and percentage. Nothing here has a
numeric default except zero: the numbers are Rudra's data.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.ledger.models import Frozen
from app.ledger.money import ZERO, NonNegativeMoney, Percent, PositiveDecimal, percent_of


class FixedCap(Frozen):
    kind: Literal["fixed"]
    amount: NonNegativeMoney


class PercentCap(Frozen):
    kind: Literal["percent"]
    percent: Percent


Cap = Annotated[FixedCap | PercentCap, Field(discriminator="kind")]


def cap_amount(cap: FixedCap | PercentCap, net_pay: Decimal) -> Decimal:
    """The cap in dollars for this paycheque. Percentages round down to the cent (P4)."""
    if isinstance(cap, FixedCap):
        return cap.amount
    return percent_of(cap.percent, net_pay)


class MiddleBucket(StrEnum):
    BUFFER = "buffer"
    EMERGENCY_FUND = "emergency_fund"
    GOALS = "goals"
    INVESTING = "investing"
    EXPLORE = "explore"


DEFAULT_ORDER: tuple[MiddleBucket, ...] = (
    MiddleBucket.BUFFER,
    MiddleBucket.EMERGENCY_FUND,
    MiddleBucket.GOALS,
    MiddleBucket.INVESTING,
    MiddleBucket.EXPLORE,
)


class BufferPolicy(Frozen):
    """Top up the account with role ``buffer`` to ``target``."""

    target: NonNegativeMoney


class EmergencyFundPolicy(Frozen):
    """Target = ``months`` x monthly essentials (P6), funded up to ``per_pay_cap`` each pay."""

    months: PositiveDecimal
    per_pay_cap: Cap


class GoalsPolicy(Frozen):
    #: P10: per-pay amount for goals without a target date (zero means "not funded automatically").
    undated_per_pay: NonNegativeMoney = ZERO
    #: P10: optional cap per pay for goals past their date (None = the whole remaining amount).
    overdue_per_pay_cap: Cap | None = None


class InvestingPolicy(Frozen):
    """P12: up to ``per_pay_limit`` split across ``targets`` (account ids) in order, room-capped."""

    per_pay_limit: Cap
    targets: tuple[str, ...] = Field(min_length=1)


class ExplorePolicy(Frozen):
    percent: Percent
    account_id: str


class SurplusPolicy(Frozen):
    """Optional: cap spending and send the excess to an account (never registered)."""

    spending_cap: Cap
    redirect_to: str


class AllocationPolicy(Frozen):
    version: int = Field(ge=1)
    order: tuple[MiddleBucket, ...] = DEFAULT_ORDER
    buffer: BufferPolicy | None = None
    emergency_fund: EmergencyFundPolicy | None = None
    goals: GoalsPolicy = GoalsPolicy()
    investing: InvestingPolicy | None = None
    explore: ExplorePolicy | None = None
    surplus: SurplusPolicy | None = None

    @model_validator(mode="after")
    def _order(self) -> Self:
        if sorted(self.order) != sorted(DEFAULT_ORDER):
            raise ValueError(f"order must be a permutation of {[b.value for b in DEFAULT_ORDER]}")
        return self


__all__ = [
    "AllocationPolicy",
    "BufferPolicy",
    "Cap",
    "EmergencyFundPolicy",
    "ExplorePolicy",
    "FixedCap",
    "GoalsPolicy",
    "InvestingPolicy",
    "MiddleBucket",
    "PercentCap",
    "SurplusPolicy",
    "cap_amount",
]
