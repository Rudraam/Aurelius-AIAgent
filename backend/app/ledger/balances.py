"""Projected balances and reconciliation drift (P8). Pure.

Projected balance = the latest balance entry plus approved plan lines into the account dated after
it. Approving moves no money (invariant 4), so the projection *assumes* Rudra made each approved
transfer. When a new balance entry arrives, ``reconcile`` measures the gap (actual - projected) so it
can be reported and stored. From then on, projection starts from the new, actual entry.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal, localcontext
from typing import Self

from pydantic import model_validator

from app.ledger.models import Frozen
from app.ledger.money import ZERO, Money, ledger_context


class BalanceEntry(Frozen):
    account_id: str
    amount: Money
    as_of: date


class Flow(Frozen):
    """An approved plan line into (positive) or out of (negative) an account."""

    account_id: str
    on: date
    amount: Money


class Drift(Frozen):
    account_id: str
    as_of: date
    projected: Money
    actual: Money
    #: actual - projected. Negative: less money than the approved plans assumed.
    difference: Money

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.actual - self.projected != self.difference:
            raise ValueError("difference must equal actual - projected")
        return self


def project_balance(
    account_id: str, latest: BalanceEntry | None, flows: Iterable[Flow], *, through: date | None = None
) -> Decimal:
    """Latest entry plus the account's flows dated after it (and on or before ``through``, if set)."""
    if latest is not None and latest.account_id != account_id:
        raise ValueError("balance entry belongs to another account")
    base = latest.amount if latest is not None else ZERO
    with localcontext(ledger_context()):
        return base + sum(
            (
                f.amount
                for f in flows
                if f.account_id == account_id
                and (latest is None or f.on > latest.as_of)
                and (through is None or f.on <= through)
            ),
            ZERO,
        )


def reconcile(previous: BalanceEntry | None, flows: Iterable[Flow], new: BalanceEntry) -> Drift:
    """The drift a new balance entry reveals against the projection up to its date."""
    projected = project_balance(new.account_id, previous, flows, through=new.as_of)
    with localcontext(ledger_context()):
        difference = new.amount - projected
    return Drift(
        account_id=new.account_id,
        as_of=new.as_of,
        projected=projected,
        actual=new.amount,
        difference=difference,
    )
