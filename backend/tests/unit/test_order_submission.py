"""Invariant 5: create is called once, never retried; auth failures -> submit_failed; resubmission is gated."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.domain.order_state import ALLOWED_TRANSITIONS, TERMINAL, IllegalTransition, OrderDraft, OrderStatus
from app.integrations.questrade.errors import AuthExpired, ReauthRequired
from app.services.order_submission import (
    CreateOutcome,
    InstructionSummary,
    ResubmissionBlocked,
    authorize_resubmission,
    submit_draft,
)

S = OrderStatus
ACCOUNT = "11111111-1111-1111-1111-111111111111"


class CountingCreate:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    async def __call__(self) -> CreateOutcome:
        self.calls += 1
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        return self.behaviour


def draft() -> OrderDraft:
    return OrderDraft(id="d1", account_id=ACCOUNT, idempotency_key="k1", status=S.PREVIEWED)


# --------------------------------------------------------------------------- state machine


def test_terminal_states_have_no_exits():
    assert {S.REJECTED_BY_GUARDRAIL, S.ABANDONED, S.SUCCEEDED, S.FAILED, S.DENIED, S.EXPIRED} == TERMINAL


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (S.PREVIEWED, S.PENDING_APPROVAL),  # must pass through submitted
        (S.SUBMIT_FAILED, S.SUBMITTED),  # resubmission goes back through previewed (fresh preview + confirm)
        (S.SUBMIT_FAILED, S.PENDING_APPROVAL),
        (S.SUCCEEDED, S.SUBMITTED),
        (S.INTENT, S.SUBMITTED),
    ],
)
def test_illegal_transitions_raise(frm, to):
    d = OrderDraft(id="x", account_id=ACCOUNT, idempotency_key="k", status=frm)
    with pytest.raises(IllegalTransition):
        d.transition(to, "test", draft_now())
    assert d.status == frm and d.events == []


def test_every_status_has_a_transition_entry():
    assert set(ALLOWED_TRANSITIONS) == set(OrderStatus)


def draft_now():
    from datetime import UTC, datetime

    return datetime(2026, 11, 9, 15, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- submit


async def test_success_goes_to_pending_approval_with_one_call(clock):
    create = CountingCreate(CreateOutcome("inst-1", S.PENDING_APPROVAL))
    d = await submit_draft(draft(), create, clock)
    assert create.calls == 1
    assert d.status == S.PENDING_APPROVAL and d.instruction_id == "inst-1"
    assert [e.to_status for e in d.events] == [S.SUBMITTED, S.PENDING_APPROVAL]


@pytest.mark.parametrize(
    "err",
    [
        AuthExpired("approval call got 401"),
        ReauthRequired("insufficient scope", scope="mcp:brokerage.orders.all"),
        ExceptionGroup("sdk task group", [RuntimeError("wrapper"), AuthExpired("approval call got 401")]),
    ],
    ids=["auth-expired", "reauth-scope", "wrapped-in-group"],
)
async def test_auth_failure_moves_to_submit_failed_without_retry(clock, err):
    create = CountingCreate(err)
    d = await submit_draft(draft(), create, clock)
    assert create.calls == 1
    assert d.status == S.SUBMIT_FAILED
    assert d.instruction_id is None


async def test_scope_failure_records_required_scope(clock):
    d = await submit_draft(draft(), CountingCreate(ReauthRequired("x", scope="mcp:brokerage.orders.all")), clock)
    assert d.events[-1].payload["required_scope"] == "mcp:brokerage.orders.all"


@pytest.mark.parametrize("err", [TimeoutError("read timed out"), ConnectionResetError(), ValueError("bad json")])
async def test_ambiguous_failure_moves_to_unknown_without_retry(clock, err):
    create = CountingCreate(err)
    d = await submit_draft(draft(), create, clock)
    assert create.calls == 1
    assert d.status == S.UNKNOWN


async def test_cancellation_marks_unknown_and_propagates(clock):
    create = CountingCreate(asyncio.CancelledError())
    d = draft()
    with pytest.raises(asyncio.CancelledError):
        await submit_draft(d, create, clock)
    assert create.calls == 1 and d.status == S.UNKNOWN


async def test_cannot_submit_twice(clock):
    d = await submit_draft(draft(), CountingCreate(CreateOutcome("i", S.PENDING_APPROVAL)), clock)
    second = CountingCreate(CreateOutcome("i2", S.PENDING_APPROVAL))
    with pytest.raises(IllegalTransition):
        await submit_draft(d, second, clock)
    assert second.calls == 0


async def test_cannot_submit_from_submit_failed_directly(clock):
    d = await submit_draft(draft(), CountingCreate(AuthExpired("401")), clock)
    again = CountingCreate(CreateOutcome("i", S.PENDING_APPROVAL))
    with pytest.raises(IllegalTransition):
        await submit_draft(d, again, clock)
    assert again.calls == 0


# --------------------------------------------------------------------------- resubmission gate


class FakeInstructions:
    def __init__(self, items=(), error: Exception | None = None):
        self.items, self.error, self.calls = list(items), error, 0

    async def __call__(self, account_id: str):
        self.calls += 1
        if self.error:
            raise self.error
        return self.items


async def _failed(clock) -> OrderDraft:
    return await submit_draft(draft(), CountingCreate(AuthExpired("401")), clock)


async def test_resubmission_requires_human(clock):
    d = await _failed(clock)
    li = FakeInstructions()
    with pytest.raises(ResubmissionBlocked, match="human"):
        await authorize_resubmission(d, confirmed_by_human=False, list_instructions=li, clock=clock)
    assert li.calls == 0 and d.status == S.SUBMIT_FAILED


async def test_clean_check_returns_to_previewed(clock):
    d = await _failed(clock)
    old = InstructionSummary("old", ACCOUNT, clock() - timedelta(hours=3))
    other_account = InstructionSummary("x", "22222222-2222-2222-2222-222222222222", clock())
    li = FakeInstructions([old, other_account])
    await authorize_resubmission(d, confirmed_by_human=True, list_instructions=li, clock=clock)
    assert li.calls == 1
    assert d.status == S.PREVIEWED


@pytest.mark.parametrize(
    "created_offset", [timedelta(0), timedelta(seconds=-60), None], ids=["same-time", "skew", "no-ts"]
)
async def test_instruction_since_attempt_blocks_and_moves_to_unknown(clock, created_offset):
    d = await _failed(clock)
    created = None if created_offset is None else d.submitted_at + created_offset
    li = FakeInstructions([InstructionSummary("maybe-mine", ACCOUNT, created)])
    with pytest.raises(ResubmissionBlocked):
        await authorize_resubmission(d, confirmed_by_human=True, list_instructions=li, clock=clock)
    assert d.status == S.UNKNOWN
    assert d.events[-1].payload["instruction_ids"] == ["maybe-mine"]


async def test_list_instructions_failure_keeps_submit_failed(clock):
    d = await _failed(clock)
    with pytest.raises(AuthExpired):
        await authorize_resubmission(
            d, confirmed_by_human=True, list_instructions=FakeInstructions(error=AuthExpired("down")), clock=clock
        )
    assert d.status == S.SUBMIT_FAILED


@pytest.mark.parametrize("status", [S.PREVIEWED, S.PENDING_APPROVAL, S.UNKNOWN, S.SUCCEEDED])
async def test_resubmission_only_from_submit_failed(clock, status):
    d = OrderDraft(id="x", account_id=ACCOUNT, idempotency_key="k", status=status)
    with pytest.raises(ResubmissionBlocked):
        await authorize_resubmission(d, confirmed_by_human=True, list_instructions=FakeInstructions(), clock=clock)
