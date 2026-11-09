"""Order draft state machine. The only place transitions are defined; illegal transitions raise.

intent ─guardrails─► rejected_by_guardrail (terminal)
  └─► previewed ─confirm─► submitted ─► pending_approval ─► executing ─► succeeded / failed
         │                   │   │              └──────────► succeeded / failed / denied / expired
         └─► abandoned       │   └─ timeout / ambiguous ─► unknown ─reconcile─► real status
                             └─ auth failure (401 etc.) ─► submit_failed
submit_failed ─(human resubmits AND list_instructions shows nothing created)─► previewed
submit_failed ─(list_instructions shows an instruction since the attempt)──────► unknown
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class OrderStatus(StrEnum):
    INTENT = "intent"
    REJECTED_BY_GUARDRAIL = "rejected_by_guardrail"
    PREVIEWED = "previewed"
    ABANDONED = "abandoned"
    SUBMITTED = "submitted"
    SUBMIT_FAILED = "submit_failed"
    PENDING_APPROVAL = "pending_approval"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


S = OrderStatus
_SERVER_OUTCOMES = frozenset({S.PENDING_APPROVAL, S.EXECUTING, S.SUCCEEDED, S.FAILED, S.DENIED, S.EXPIRED})

ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    S.INTENT: frozenset({S.REJECTED_BY_GUARDRAIL, S.PREVIEWED}),
    S.PREVIEWED: frozenset({S.SUBMITTED, S.ABANDONED}),
    S.SUBMITTED: frozenset({S.PENDING_APPROVAL, S.SUBMIT_FAILED, S.UNKNOWN}),
    # previewed: human-authorized resubmission after a clean list_instructions check (fresh preview required)
    # unknown: the check found an instruction created since the attempt -> reconcile instead
    S.SUBMIT_FAILED: frozenset({S.PREVIEWED, S.UNKNOWN, S.ABANDONED}),
    S.PENDING_APPROVAL: frozenset({S.EXECUTING, S.SUCCEEDED, S.FAILED, S.DENIED, S.EXPIRED}),
    S.EXECUTING: frozenset({S.SUCCEEDED, S.FAILED}),
    # reconcile resolves to a server outcome, or to submit_failed when nothing was created
    S.UNKNOWN: _SERVER_OUTCOMES | {S.SUBMIT_FAILED},
    S.REJECTED_BY_GUARDRAIL: frozenset(),
    S.ABANDONED: frozenset(),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset(),
    S.DENIED: frozenset(),
    S.EXPIRED: frozenset(),
}

TERMINAL = frozenset(s for s, nxt in ALLOWED_TRANSITIONS.items() if not nxt)


class IllegalTransition(Exception):
    def __init__(self, current: OrderStatus, target: OrderStatus) -> None:
        super().__init__(f"illegal order transition {current} -> {target}")
        self.current, self.target = current, target


def check_transition(current: OrderStatus, target: OrderStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise IllegalTransition(current, target)


@dataclass(frozen=True, slots=True)
class DraftEvent:
    from_status: OrderStatus
    to_status: OrderStatus
    reason: str
    at: datetime
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderDraft:
    """In-memory view of an ``order_draft`` row plus its append-only ``order_draft_event`` rows."""

    id: str
    account_id: str
    idempotency_key: str
    status: OrderStatus = S.PREVIEWED
    #: Arguments sent to preview_order_instruction, their canonical hash, and when the preview ran.
    preview_args: dict[str, Any] | None = None
    preview_args_hash: str | None = None
    previewed_at: datetime | None = None
    instruction_id: str | None = None
    submitted_at: datetime | None = None
    terminal_at: datetime | None = None
    events: list[DraftEvent] = field(default_factory=list)

    def clear_preview(self) -> None:
        self.preview_args = None
        self.preview_args_hash = None
        self.previewed_at = None

    def transition(self, target: OrderStatus, reason: str, at: datetime, **payload: Any) -> None:
        check_transition(self.status, target)
        self.events.append(DraftEvent(self.status, target, reason, at, dict(payload)))
        self.status = target
        if target in TERMINAL:
            self.terminal_at = at
