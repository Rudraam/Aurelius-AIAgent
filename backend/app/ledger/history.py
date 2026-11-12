"""What stored plan history counts for (P8, P9, P29-P31). Pure.

The service loads plan lines with their plan's lifecycle state and passes them here. **Only lines of
approved plans count.** Proposed and revised versions never moved money, and a voided plan's lines stop
counting toward balances, room, goals and sinking funds the moment it is voided (P30).

- Projected balances count only *kept* money (P29): the buffer, emergency fund, goals, investing,
  explore and surplus buckets, plus sinking-fund lines (money set aside until the bill is due).
  A bill due in the window and a budget line pass through the account and are spent, so they don't.
- Contribution room counts every approved line into a registered account, whatever its bucket
  (invariant 7).
- A goal's saved amount is its approved goal lines + the amount saved before Aurelius + signed manual
  adjustments (P31).
- A sinking bill's funded amount is the approved sinking lines for that bill's current occurrence (P7).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal, localcontext
from enum import StrEnum

from pydantic import Field

from app.ledger.balances import Flow
from app.ledger.models import REGISTERED, AccountKind, Frozen
from app.ledger.money import ZERO, Money, NonNegativeMoney, ledger_context
from app.ledger.plan import Bucket
from app.ledger.room import MovementType, RoomMovement

SINKING_REASON = "sinking_fund"

#: Buckets whose money stays in the destination account (P29).
KEPT_BUCKETS: frozenset[Bucket] = frozenset(
    {Bucket.BUFFER, Bucket.EMERGENCY_FUND, Bucket.GOALS, Bucket.INVESTING, Bucket.EXPLORE, Bucket.SURPLUS}
)


class PlanState(StrEnum):
    """Where a stored plan is in its lifecycle (spec section 5, P30)."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    #: Replaced by a revision before approval.
    REVISED = "revised"
    #: Voided after approval (or cascaded from a void while still proposed).
    VOIDED = "voided"


class HistoryLine(Frozen):
    """One stored plan line, with its plan's state and payday."""

    plan_id: int
    state: PlanState
    payday: date
    line_id: str = Field(min_length=1)
    bucket: Bucket
    account_id: str | None = None
    amount: NonNegativeMoney
    reason_code: str


class GoalAdjustment(Frozen):
    """A signed manual correction to a goal's saved amount (P31)."""

    goal_id: str
    amount: Money
    on: date


def goal_line_id(goal_id: str) -> str:
    return f"goal:{goal_id}"


def bill_line_id(bill_id: str, due: date) -> str:
    return f"bill:{bill_id}:{due.isoformat()}"


def counts(line: HistoryLine) -> bool:
    return line.state is PlanState.APPROVED


def is_kept(bucket: Bucket, reason_code: str) -> bool:
    return bucket in KEPT_BUCKETS or (bucket is Bucket.BILLS and reason_code == SINKING_REASON)


def _sum(values: Iterable[Decimal]) -> Decimal:
    with localcontext(ledger_context()):
        return sum(values, ZERO)


def account_flows(lines: Iterable[HistoryLine]) -> tuple[Flow, ...]:
    """Approved kept-money lines as flows into their accounts, dated on their payday."""
    return tuple(
        Flow(account_id=line.account_id, on=line.payday, amount=line.amount)
        for line in lines
        if counts(line) and line.account_id is not None and is_kept(line.bucket, line.reason_code)
    )


def room_contributions(lines: Iterable[HistoryLine], kinds: Mapping[str, AccountKind]) -> tuple[RoomMovement, ...]:
    """Approved lines into registered accounts, as contributions on their payday (invariant 7)."""
    out: list[RoomMovement] = []
    for line in lines:
        if not counts(line) or line.account_id is None or line.amount <= ZERO:
            continue
        kind = kinds.get(line.account_id)
        if kind is None:
            raise ValueError(f"plan line {line.line_id} names unknown account {line.account_id!r}")
        if kind in REGISTERED:
            out.append(RoomMovement(kind=kind, on=line.payday, amount=line.amount, movement=MovementType.CONTRIBUTION))
    return tuple(out)


def goal_saved(
    goal_id: str,
    saved_before_aurelius: Decimal,
    adjustments: Iterable[GoalAdjustment],
    lines: Iterable[HistoryLine],
    *,
    through: date,
) -> Decimal:
    """Saved toward a goal as of ``through`` (inclusive). May be negative if adjustments say so."""
    wanted = goal_line_id(goal_id)
    return _sum(
        [
            saved_before_aurelius,
            *(a.amount for a in adjustments if a.goal_id == goal_id and a.on <= through),
            *(line.amount for line in lines if counts(line) and line.line_id == wanted and line.payday <= through),
        ]
    )


def sinking_funded(bill_id: str, due: date, lines: Iterable[HistoryLine]) -> Decimal:
    """Approved sinking lines for the occurrence of ``bill_id`` due on ``due``."""
    wanted = bill_line_id(bill_id, due)
    return _sum(
        line.amount for line in lines if counts(line) and line.line_id == wanted and line.reason_code == SINKING_REASON
    )
