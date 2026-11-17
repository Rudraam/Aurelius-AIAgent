"""Balance entries, projected balances and drift (P8, P36, P40).

- One entry per (account, as-of date). An entry dated before the account's latest entry is refused.
- An account's first entry is the baseline: no drift.
- Every later entry is reconciled against the projection from the previous entry through its own date
  (kept money only, P36), and one drift row is stored for it, even when the difference is zero.

Entries and drift are append-only. The entry and its drift are written in one savepoint, so either
both are stored or neither is.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import AccountRow, BalanceDriftRow, BalanceEntryRow
from app.ledger.balances import BalanceEntry, Drift, project_balance, reconcile
from app.ledger.history import account_flows
from app.services.ledger.clock import require_utc
from app.services.ledger.errors import Conflict, NotFound
from app.services.ledger.history import approved_lines


class BalanceSource(StrEnum):
    MANUAL = "manual"
    QUESTRADE_SYNC = "questrade_sync"


def _entry(row: BalanceEntryRow) -> BalanceEntry:
    return BalanceEntry(account_id=row.account_id, amount=row.amount, as_of=row.as_of)


def _drift(row: BalanceDriftRow) -> Drift:
    return Drift(
        account_id=row.account_id,
        as_of=row.as_of,
        projected=row.projected,
        actual=row.actual,
        difference=row.difference,
    )


async def _require_account(session: AsyncSession, account_id: str) -> None:
    if await session.get(AccountRow, account_id) is None:
        raise NotFound(f"no account {account_id!r}")


async def _latest_row(session: AsyncSession, account_id: str, *, before: date | None = None) -> BalanceEntryRow | None:
    query = select(BalanceEntryRow).where(BalanceEntryRow.account_id == account_id)
    if before is not None:
        query = query.where(BalanceEntryRow.as_of < before)
    row: BalanceEntryRow | None = await session.scalar(query.order_by(BalanceEntryRow.as_of.desc()).limit(1))
    return row


async def latest_balance(session: AsyncSession, account_id: str) -> BalanceEntry | None:
    await _require_account(session, account_id)
    row = await _latest_row(session, account_id)
    return _entry(row) if row is not None else None


async def balance_history(session: AsyncSession, account_id: str) -> list[BalanceEntry]:
    await _require_account(session, account_id)
    query = select(BalanceEntryRow).where(BalanceEntryRow.account_id == account_id).order_by(BalanceEntryRow.as_of)
    return [_entry(row) for row in (await session.scalars(query)).all()]


async def projected_balance(session: AsyncSession, account_id: str, *, through: date) -> Decimal:
    """The latest entry on or before ``through``, plus approved kept-money lines after it (P36)."""
    await _require_account(session, account_id)
    row = await _latest_row(session, account_id, before=through + timedelta(days=1))
    base = _entry(row) if row is not None else None
    lines = await approved_lines(session, account_id=account_id, through=through)
    return project_balance(account_id, base, account_flows(lines), through=through)


async def record_balance(
    session: AsyncSession,
    entry: BalanceEntry,
    *,
    source: BalanceSource = BalanceSource.MANUAL,
    now: datetime,
) -> Drift | None:
    """Store a balance entry and, unless it is the account's first, its drift (P40)."""
    require_utc(now)
    await _require_account(session, entry.account_id)
    previous_row = await _latest_row(session, entry.account_id)
    if previous_row is not None and entry.as_of <= previous_row.as_of:
        when = "already has an entry for" if entry.as_of == previous_row.as_of else "has a later entry than"
        raise Conflict(
            f"account {entry.account_id} {when} {entry.as_of.isoformat()}"
            f" (latest: {previous_row.as_of.isoformat()}); balances can't be backdated"
        )

    drift: Drift | None = None
    if previous_row is not None:
        lines = await approved_lines(session, account_id=entry.account_id, through=entry.as_of)
        drift = reconcile(_entry(previous_row), account_flows(lines), entry)

    try:
        async with session.begin_nested():
            row = BalanceEntryRow(
                account_id=entry.account_id,
                amount=entry.amount,
                as_of=entry.as_of,
                source=source.value,
                created_at=now,
            )
            session.add(row)
            await session.flush()
            if drift is not None:
                session.add(
                    BalanceDriftRow(
                        balance_entry_id=row.id,
                        account_id=drift.account_id,
                        as_of=drift.as_of,
                        projected=drift.projected,
                        actual=drift.actual,
                        difference=drift.difference,
                        created_at=now,
                    )
                )
    except IntegrityError:
        raise Conflict(f"account {entry.account_id} already has an entry for {entry.as_of.isoformat()}") from None
    return drift


async def drifts(
    session: AsyncSession, *, account_id: str | None = None, start: date | None = None, end: date | None = None
) -> list[Drift]:
    """Stored drift records by as-of date, optionally for one account and within [start, end]."""
    query = select(BalanceDriftRow).order_by(BalanceDriftRow.as_of, BalanceDriftRow.id)
    if account_id is not None:
        query = query.where(BalanceDriftRow.account_id == account_id)
    if start is not None:
        query = query.where(BalanceDriftRow.as_of >= start)
    if end is not None:
        query = query.where(BalanceDriftRow.as_of <= end)
    return [_drift(row) for row in (await session.scalars(query)).all()]
