"""Submitting a confirmed draft, and the human-gated path after a failed submit.

Gates checked by ``check_submittable`` before any state change or call. Each failure raises a
``SubmissionBlocked`` subclass, leaves the draft untouched, and calls nothing:
- invariant 2: ``trading_enabled`` must be true, so with trading off ``create`` is unreachable;
- the draft must be ``previewed`` and the operation must be ``create`` (no modify or cancel);
- invariant 4: a stored preview must exist, the hash of the args about to be sent must equal the
  stored preview hash, and the hash the human confirmed must equal it too;
- the preview must be younger than ``PREVIEW_MAX_AGE`` (5 minutes).

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
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from app.domain.order_args import order_args_hash
from app.domain.order_state import OrderDraft, OrderStatus, check_transition
from app.integrations.questrade.errors import AuthExpired, ReauthRequired, find_in_chain

Clock = Callable[[], datetime]

PREVIEW_MAX_AGE = timedelta(minutes=5)
#: The "created since the attempt" window starts this long before the attempt, to tolerate clock
#: skew between the VM and Questrade.
RESUBMIT_SKEW_TOLERANCE = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class SubmitPolicy:
    trading_enabled: bool  # AURELIUS_TRADING_ENABLED; defaults to false in config
    max_preview_age: timedelta = PREVIEW_MAX_AGE


class SubmissionBlocked(Exception):
    """Base class: the submit was refused before any state change or Questrade call."""


class TradingDisabled(SubmissionBlocked):
    pass


class OperationNotAllowed(SubmissionBlocked):
    pass


class PreviewMissing(SubmissionBlocked):
    pass


class PreviewMismatch(SubmissionBlocked):
    pass


class PreviewStale(SubmissionBlocked):
    pass


@dataclass(frozen=True, slots=True)
class CreateOutcome:
    instruction_id: str
    status: OrderStatus


@dataclass(frozen=True, slots=True)
class InstructionSummary:
    instruction_id: str
    account_id: str
    created_at: datetime | None


#: Performs exactly one ``create_order_instruction`` call on the approval session, with exactly these args.
CreateFn = Callable[[Mapping[str, Any]], Awaitable[CreateOutcome]]
ListInstructionsFn = Callable[[str], Awaitable[Sequence[InstructionSummary]]]


class ResubmissionBlocked(Exception):
    pass


def check_submittable(
    draft: OrderDraft,
    create_args: Mapping[str, Any],
    confirmed_preview_hash: str,
    policy: SubmitPolicy,
    now: datetime,
) -> None:
    """Raise ``SubmissionBlocked`` (or ``IllegalTransition``) unless every submit gate passes. Pure."""
    if not policy.trading_enabled:
        raise TradingDisabled("AURELIUS_TRADING_ENABLED is false; order submission is disabled")
    check_transition(draft.status, OrderStatus.SUBMITTED)
    if create_args.get("operation") != "create":
        raise OperationNotAllowed(f"operation must be 'create', got {create_args.get('operation')!r}")
    if draft.preview_args_hash is None or draft.previewed_at is None:
        raise PreviewMissing("no stored preview for this draft; preview before submitting")
    if not secrets.compare_digest(order_args_hash(create_args), draft.preview_args_hash):
        raise PreviewMismatch("create arguments do not match the stored preview")
    if not secrets.compare_digest(confirmed_preview_hash, draft.preview_args_hash):
        raise PreviewMismatch("the confirmed preview hash does not match the stored preview")
    age = now - draft.previewed_at
    if age < timedelta(0) or age >= policy.max_preview_age:
        raise PreviewStale(f"preview is {int(age.total_seconds())}s old; preview again (max {policy.max_preview_age})")


async def submit_draft(
    draft: OrderDraft,
    *,
    create_args: Mapping[str, Any],
    confirmed_preview_hash: str,
    create: CreateFn,
    policy: SubmitPolicy,
    clock: Clock,
) -> OrderDraft:
    sent = MappingProxyType(dict(create_args))  # freeze: what is checked is exactly what is sent
    now = clock()
    check_submittable(draft, sent, confirmed_preview_hash, policy, now)
    draft.transition(OrderStatus.SUBMITTED, "confirmed in Aurelius; calling create_order_instruction once", now)
    draft.submitted_at = now
    try:
        outcome = await create(sent)
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
    lookback: timedelta = RESUBMIT_SKEW_TOLERANCE,
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
        "human-authorized resubmission; list_instructions clean; fresh preview required",
        clock(),
        checked_since=since.isoformat(),
    )
    draft.clear_preview()  # structurally forces a new preview + confirmation before the next submit
    return draft
