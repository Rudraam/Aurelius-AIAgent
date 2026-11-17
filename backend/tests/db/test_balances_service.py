"""Balance entries, projection and drift (P8, P36, P40) against the migrated schema. Fictional data only."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import PaychequeRow, PlanLineRow, PlanRow
from app.ledger.balances import BalanceEntry, Drift
from app.ledger.history import SINKING_REASON
from app.ledger.models import AccountKind, PayFrequency
from app.services.ledger import balances, config, records
from app.services.ledger.balances import BalanceSource
from app.services.ledger.config import AccountSpec, IncomeSourceSpec
from app.services.ledger.errors import Conflict, NotFound
from tests.unit.ledger.factories import standard_policy

NOW = datetime(2026, 11, 15, 17, 0, tzinfo=UTC)
D = Decimal


async def setup_config(session: AsyncSession) -> None:
    for spec in (
        AccountSpec(id="chq", kind=AccountKind.CHEQUING, currency="CAD"),
        AccountSpec(id="hisa", kind=AccountKind.HISA, currency="CAD"),
        IncomeSourceSpec(id="job", name="Example", frequency=PayFrequency.BIWEEKLY, anchor=date(2026, 11, 6)),
    ):
        await config.create(session, spec, now=NOW)
    await records.add_policy(session, standard_policy(), now=NOW)


async def add_plan(session: AsyncSession, payday: date, state: str, lines: list[tuple[str, str, str, str, str]]) -> int:
    """Insert a paycheque and a plan in ``state`` with (line_id, bucket, account, amount, reason) lines."""
    paycheque = PaychequeRow(income_source_id="job", payday=payday, net_pay=D("2000.00"), created_at=NOW)
    session.add(paycheque)
    await session.flush()
    plan = PlanRow(
        paycheque_id=paycheque.id, state=state, status="ok", policy_version=1, inputs_hash="0" * 64,
        snapshot=b"{}", plan_json=b"{}", payday=payday, net_pay=D("2000.00"), created_at=NOW,
    )  # fmt: skip
    session.add(plan)
    await session.flush()
    for position, (line_id, bucket, account, amount, reason) in enumerate(lines):
        session.add(
            PlanLineRow(
                plan_id=plan.id, line_id=line_id, position=position, bucket=bucket, account_id=account,
                amount=D(amount), reason_code=reason, reason_params={},
            )
        )  # fmt: skip
    await session.flush()
    return plan.id


def entry(amount: str, as_of: date, account: str = "hisa") -> BalanceEntry:
    return BalanceEntry(account_id=account, amount=D(amount), as_of=as_of)


async def test_first_entry_is_the_baseline(session: AsyncSession) -> None:
    await setup_config(session)
    assert await balances.record_balance(session, entry("1000.00", date(2026, 11, 1)), now=NOW) is None
    assert await balances.drifts(session) == []
    assert await balances.latest_balance(session, "hisa") == entry("1000.00", date(2026, 11, 1))


async def test_drift_counts_only_approved_kept_lines(session: AsyncSession) -> None:
    await setup_config(session)
    await balances.record_balance(session, entry("1000.00", date(2026, 11, 1)), now=NOW)
    await add_plan(session, date(2026, 11, 6), "approved", [
        ("ef", "emergency_fund", "hisa", "200.00", "ef_contribution"),
        ("goal:trip", "goals", "hisa", "50.00", "goal_contribution"),
        ("bill:rent:2026-12-01", "bills", "hisa", "300.00", SINKING_REASON),
        ("bill:phone:2026-11-07", "bills", "hisa", "40.00", "bill_due"),  # spent: not kept
        ("groceries", "essentials", "hisa", "100.00", "budget_line"),  # spent: not kept
    ])  # fmt: skip
    await add_plan(session, date(2026, 11, 20), "proposed", [("ef", "emergency_fund", "hisa", "999.00", "x")])
    await add_plan(session, date(2026, 12, 4), "approved", [("ef", "emergency_fund", "hisa", "77.00", "x")])

    # Through 2026-11-30: 1000 + 200 + 50 + 300 (kept lines of the approved 11-06 plan only).
    assert await balances.projected_balance(session, "hisa", through=date(2026, 11, 30)) == D("1550.00")
    drift = await balances.record_balance(session, entry("1540.00", date(2026, 11, 30)), now=NOW)
    expected = Drift(
        account_id="hisa", as_of=date(2026, 11, 30), projected=D("1550.00"), actual=D("1540.00"),
        difference=D("-10.00"),
    )  # fmt: skip
    assert drift == expected
    assert await balances.drifts(session, account_id="hisa") == [expected]

    # The projection now starts from the new, actual entry.
    assert await balances.projected_balance(session, "hisa", through=date(2026, 12, 31)) == D("1617.00")


async def test_voided_plan_lines_stop_counting(session: AsyncSession) -> None:
    await setup_config(session)
    await balances.record_balance(session, entry("100.00", date(2026, 11, 1)), now=NOW)
    plan_id = await add_plan(session, date(2026, 11, 6), "approved", [("ef", "emergency_fund", "hisa", "20.00", "x")])
    assert await balances.projected_balance(session, "hisa", through=date(2026, 11, 30)) == D("120.00")
    await session.execute(text("UPDATE allocation_plan SET state = 'voided' WHERE id = :id"), {"id": plan_id})
    assert await balances.projected_balance(session, "hisa", through=date(2026, 11, 30)) == D("100.00")


async def test_zero_drift_is_stored(session: AsyncSession) -> None:
    await setup_config(session)
    await balances.record_balance(session, entry("500.00", date(2026, 11, 1)), now=NOW)
    drift = await balances.record_balance(
        session, entry("500.00", date(2026, 11, 8)), source=BalanceSource.QUESTRADE_SYNC, now=NOW
    )
    assert drift is not None and drift.difference == D("0.00")
    assert len(await balances.drifts(session)) == 1


async def test_backdated_and_duplicate_entries_conflict(session: AsyncSession) -> None:
    await setup_config(session)
    await balances.record_balance(session, entry("500.00", date(2026, 11, 8)), now=NOW)
    with pytest.raises(Conflict, match="already has an entry"):
        await balances.record_balance(session, entry("510.00", date(2026, 11, 8)), now=NOW)
    with pytest.raises(Conflict, match="later entry"):
        await balances.record_balance(session, entry("490.00", date(2026, 11, 1)), now=NOW)
    # Other accounts are independent.
    await balances.record_balance(session, entry("10.00", date(2026, 11, 1), account="chq"), now=NOW)
    await session.commit()
    assert [e.as_of for e in await balances.balance_history(session, "hisa")] == [date(2026, 11, 8)]


async def test_drift_window_filter(session: AsyncSession) -> None:
    await setup_config(session)
    for day, amount in ((1, "100.00"), (8, "110.00"), (15, "105.00")):
        await balances.record_balance(session, entry(amount, date(2026, 11, day)), now=NOW)
    in_window = await balances.drifts(session, start=date(2026, 11, 10), end=date(2026, 11, 30))
    assert [d.difference for d in in_window] == [D("-5.00")]


async def test_unknown_account_is_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFound, match="no account"):
        await balances.record_balance(session, entry("1.00", date(2026, 11, 1), account="nope"), now=NOW)
    with pytest.raises(NotFound):
        await balances.projected_balance(session, "nope", through=date(2026, 11, 1))


async def test_now_must_be_utc(session: AsyncSession) -> None:
    await setup_config(session)
    with pytest.raises(ValueError, match="UTC"):
        await balances.record_balance(session, entry("1.00", date(2026, 11, 1)), now=datetime(2026, 11, 15, 12, 0))
