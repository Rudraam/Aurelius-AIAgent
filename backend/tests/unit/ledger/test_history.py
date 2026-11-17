"""What plan history counts toward projected balances (P30, P36). Pure, no database.

The service tests in ``tests/db/test_balances_service.py`` cover the same rules end to end, but skip
without a database; these run everywhere.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.ledger.balances import Flow
from app.ledger.history import SINKING_REASON, HistoryLine, PlanState, account_flows
from app.ledger.plan import Bucket

PAYDAY = date(2026, 11, 6)


def line(
    bucket: Bucket,
    *,
    state: PlanState = PlanState.APPROVED,
    reason: str = "x",
    account: str | None = "hisa",
    amount: str = "10.00",
) -> HistoryLine:
    return HistoryLine(
        plan_id=1,
        state=state,
        payday=PAYDAY,
        line_id=f"{bucket.value}:{reason}",
        bucket=bucket,
        account_id=account,
        amount=Decimal(amount),
        reason_code=reason,
    )


@pytest.mark.parametrize("state", [PlanState.PROPOSED, PlanState.REVISED, PlanState.VOIDED])
def test_only_approved_lines_flow(state: PlanState) -> None:
    assert account_flows([line(Bucket.EMERGENCY_FUND, state=state)]) == ()
    assert account_flows([line(Bucket.EMERGENCY_FUND)]) == (
        Flow(account_id="hisa", on=PAYDAY, amount=Decimal("10.00")),
    )


@pytest.mark.parametrize(
    "bucket", [Bucket.BUFFER, Bucket.EMERGENCY_FUND, Bucket.GOALS, Bucket.INVESTING, Bucket.EXPLORE, Bucket.SURPLUS]
)
def test_kept_buckets_flow(bucket: Bucket) -> None:
    assert len(account_flows([line(bucket)])) == 1


@pytest.mark.parametrize("bucket", [Bucket.BILLS, Bucket.ESSENTIALS, Bucket.SPENDING])
def test_spent_buckets_do_not_flow(bucket: Bucket) -> None:
    assert account_flows([line(bucket, reason="bill_due")]) == ()


def test_sinking_fund_bill_lines_are_kept() -> None:
    assert len(account_flows([line(Bucket.BILLS, reason=SINKING_REASON)])) == 1


def test_lines_without_an_account_do_not_flow() -> None:
    assert account_flows([line(Bucket.SURPLUS, account=None)]) == ()
