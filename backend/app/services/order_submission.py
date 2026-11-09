"""Submitting a confirmed draft, and the human-gated path after a failed submit.

Invariant 5 (absolute): ``create_order_instruction`` is called at most once per submit, never retried.
- Auth failures (``AuthExpired``/``ReauthRequired``) happen before any tool runs (pre-refresh failure,
  or a 401/403 from the edge on the approval session), so the draft goes to ``submit_failed``.
- Any other failure (timeout, transport error, malformed reply, cancellation) is ambiguous, so the
  draft goes to ``unknown`` and is reconciled via ``list_instructions``/``get_instruction``.
- Leaving ``submit_failed`` needs a human, plus a ``list_instructions`` check that shows nothing was
  created for the account since the attempt. If the check fails, the draft stays where it is.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.order_state import OrderDraft, OrderStatus
from app.integrations.questrade.errors import AuthExpired, ReauthRequired, find_in_chain

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class CreateOutcome:
    instruction_id: str
    status: OrderStatus


@dataclass(frozen=True, slots=True)
class InstructionSummary:
    instruction_id: str
    account_id: str
    created_at: datetime | None


#: Performs exactly one ``create_order_instruction`` call on the approval session.
CreateFn = Callable[[], Awaitable[CreateOutcome]]
ListInstructionsFn = Callable[[str], Awaitable[Sequence[InstructionSummary]]]


class ResubmissionBlocked(Exception):
    pass


async def submit_draft(draft: OrderDraft, create: CreateFn, clock: Clock) -> OrderDraft:
    now = clock()
    draft.transition(OrderStatus.SUBMITTED, "confirmed in Aurelius; calling create_order_instruction once", now)
    draft.submitted_at = now
    try:
        outcome = await create()
    except asyncio.CancelledError:
        draft.transition(OrderStatus.UNKNOWN, "cancelled during create; outcome unknown", clock())
        raise
    except Exception as e:
        auth_err = find_in_chain(e, AuthExpired) or find_in_chain(e, ReauthRequired)
        if auth_err is not None:
            scope = getattr(auth_err, "scope", None)
            draft.transition(
                OrderStatus.SUBMIT_FAILED,
                f"auth failure before execution: {type(auth_err).__name__}",
                clock(),
                error=str(auth_err),
                required_scope=scope,
            )
        else:
            draft.transition(
                OrderStatus.UNKNOWN, f"ambiguous create failure: {type(e).__name__}", clock(), error=str(e)[:300]
            )
        return draft

    draft.instruction_id = outcome.instruction_id
    draft.transition(
        OrderStatus.PENDING_APPROVAL,
        "approval request sent to the Questrade app",
        clock(),
        instruction_id=outcome.instruction_id,
    )
    if outcome.status != OrderStatus.PENDING_APPROVAL:
        draft.transition(outcome.status, "status returned directly by create", clock())
    return draft


async def authorize_resubmission(
    draft: OrderDraft,
    *,
    confirmed_by_human: bool,
    list_instructions: ListInstructionsFn,
    clock: Clock,
    lookback: timedelta = timedelta(minutes=2),
) -> OrderDraft:
    """Gate a human's request to retry a ``submit_failed`` draft.

    Fails closed: any instruction for the account created since the attempt (or with no timestamp)
    moves the draft to ``unknown`` for reconciliation. A clean check returns the draft to ``previewed``;
    the normal submit path then requires a fresh preview (<5 min) and a new confirmation.
    """
    if draft.status != OrderStatus.SUBMIT_FAILED:
        raise ResubmissionBlocked(f"draft is {draft.status}, not submit_failed")
    if not confirmed_by_human:
        raise ResubmissionBlocked("resubmission is a human action")
    if draft.submitted_at is None:
        raise ResubmissionBlocked("draft has no submit timestamp; reconcile manually")

    since = draft.submitted_at - lookback
    instructions = await list_instructions(draft.account_id)  # errors propagate: draft stays submit_failed
    suspicious = [
        i.instruction_id
        for i in instructions
        if i.account_id == draft.account_id and (i.created_at is None or i.created_at >= since)
    ]
    if suspicious:
        draft.transition(
            OrderStatus.UNKNOWN,
            "list_instructions shows instruction(s) since the failed submit",
            clock(),
            instruction_ids=suspicious,
        )
        raise ResubmissionBlocked("an instruction may have been created; reconcile before any resubmission")
    draft.transition(
        OrderStatus.PREVIEWED,
        "human-authorized resubmission; list_instructions clean",
        clock(),
        checked_since=since.isoformat(),
    )
    return draft
