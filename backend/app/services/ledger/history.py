"""Load stored plan lines as ``app.ledger.history.HistoryLine`` values (P30, P36).

Only lines of **approved** plans are loaded: proposed and revised versions never moved money, and a
voided plan's lines stop counting the moment it is voided. The pure rules in ``app.ledger.history``
re-check the state, so a caller that loads more can't make them count.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import PlanLineRow, PlanRow
from app.ledger.history import HistoryLine, PlanState
from app.ledger.plan import Bucket


async def approved_lines(
    session: AsyncSession, *, account_id: str | None = None, through: date | None = None
) -> list[HistoryLine]:
    """Approved plan lines, by payday then plan then line position.

    ``account_id`` keeps only lines into that account; ``through`` keeps paydays on or before it.
    """
    query = (
        select(PlanRow.id, PlanRow.state, PlanRow.payday, PlanLineRow)
        .join(PlanLineRow, PlanLineRow.plan_id == PlanRow.id)
        .where(PlanRow.state == PlanState.APPROVED.value)
        .order_by(PlanRow.payday, PlanRow.id, PlanLineRow.position)
    )
    if account_id is not None:
        query = query.where(PlanLineRow.account_id == account_id)
    if through is not None:
        query = query.where(PlanRow.payday <= through)
    rows = (await session.execute(query)).all()
    return [
        HistoryLine(
            plan_id=plan_id,
            state=PlanState(state),
            payday=payday,
            line_id=line.line_id,
            bucket=Bucket(line.bucket),
            account_id=line.account_id,
            amount=line.amount,
            reason_code=line.reason_code,
        )
        for plan_id, state, payday, line in rows
    ]
