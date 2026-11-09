"""Submit gates (invariants 2 and 4, plus preview age) and invariant 5 (create once, never retried),
the submit_failed path, and the clock-skew-tolerant resubmission check."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.domain.market_time import TORONTO
from app.domain.order_args import order_args_hash
from app.domain.order_state import ALLOWED_TRANSITIONS, TERMINAL, IllegalTransition, OrderDraft, OrderStatus
from app.integrations.questrade.errors import AuthExpired, ReauthRequired
from app.services.order_submission import (
    PREVIEW_MAX_AGE,
    RESUBMIT_SKEW_TOLERANCE,
    CreateOutcome,
    InstructionSummary,
    OperationNotAllowed,
    PreviewMismatch,
    PreviewMissing,
    PreviewStale,
    ResubmissionBlocked,
    SubmissionBlocked,
    SubmitPolicy,
    TradingDisabled,
    authorize_resubmission,
    submit_draft,
)

S = OrderStatus
ACCOUNT = "11111111-1111-1111-1111-111111111111"
ENABLED = SubmitPolicy(trading_enabled=True)
ORDER = {
    "accountId": ACCOUNT,
    "instrument": "XEQT.TO",
    "qty": Decimal("3"),
    "side": "buy",
    "type": "limit",
    "limitPrice": Decimal("31.25"),
    "duration": "Day",
}
CREATE_ARGS = {**ORDER, "operation": "create"}
PREVIEW_HASH = order_args_hash(ORDER)


class CountingCreate:
    def __init__(self, behaviour: Any) -> None:
        self.behaviour = behaviour
        self.calls = 0
        self.sent: list[Mapping[str, Any]] = []

    async def __call__(self, args: Mapping[str, Any]) -> CreateOutcome:
        self.calls += 1
        self.sent.append(args)
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        return self.behaviour


OK = CreateOutcome("inst-1", S.PENDING_APPROVAL)


def previewed(clock, *, age: timedelta = timedelta(seconds=30)) -> OrderDraft:
    return OrderDraft(
        id="d1",
        account_id=ACCOUNT,
        idempotency_key="k1",
        status=S.PREVIEWED,
        preview_args=dict(ORDER),
        preview_args_hash=PREVIEW_HASH,
        previewed_at=clock() - age,
    )


async def submit(d: OrderDraft, create: CountingCreate, clock, *, args=None, confirmed=PREVIEW_HASH, policy=ENABLED):
    return await submit_draft(
        d,
        create_args=CREATE_ARGS if args is None else args,
        confirmed_preview_hash=confirmed,
        create=create,
        policy=policy,
        clock=clock,
    )


def assert_untouched(d: OrderDraft, create: CountingCreate) -> None:
    assert create.calls == 0
    assert d.status == S.PREVIEWED and d.events == [] and d.submitted_at is None


# --------------------------------------------------------------------------- canonical hash


def test_hash_ignores_operation_and_key_order_and_decimal_trailing_zeros():
    reordered = dict(reversed(list({**ORDER, "limitPrice": Decimal("31.250")}.items())))
    assert order_args_hash({**reordered, "operation": "create"}) == PREVIEW_HASH


@pytest.mark.parametrize(
    "field,value",
    [
        ("qty", Decimal("4")),
        ("limitPrice", Decimal("31.26")),
        ("side", "sell"),
        ("instrument", "VFV.TO"),
        ("duration", "GTC"),
        ("accountId", "other"),
    ],
)
def test_hash_changes_with_any_order_field(field, value):
    assert order_args_hash({**ORDER, field: value}) != PREVIEW_HASH


def test_hash_rejects_float():
    with pytest.raises(TypeError, match="float"):
        order_args_hash({**ORDER, "limitPrice": 31.25})


# --------------------------------------------------------------------------- gates (nothing called, nothing changed)


async def test_trading_disabled_blocks_submit(clock):
    d, create = previewed(clock), CountingCreate(OK)
    with pytest.raises(TradingDisabled):
        await submit(d, create, clock, policy=SubmitPolicy(trading_enabled=False))
    assert_untouched(d, create)


async def test_trading_disabled_wins_even_when_everything_else_is_valid_or_invalid(clock):
    """With trading off the gate refuses first, whatever the other inputs are."""
    d, create = previewed(clock, age=timedelta(hours=1)), CountingCreate(OK)
    d.clear_preview()
    with pytest.raises(TradingDisabled):
        await submit(d, create, clock, args={"operation": "cancel"}, policy=SubmitPolicy(trading_enabled=False))
    assert_untouched(d, create)


async def test_policy_defaults_are_explicit():
    with pytest.raises(TypeError):
        SubmitPolicy()  # type: ignore[call-arg]  # no implicit "enabled"
    assert SubmitPolicy(trading_enabled=True).max_preview_age == timedelta(minutes=5) == PREVIEW_MAX_AGE


async def test_missing_preview_blocks_submit(clock):
    d, create = previewed(clock), CountingCreate(OK)
    d.clear_preview()
    with pytest.raises(PreviewMissing):
        await submit(d, create, clock)
    assert_untouched(d, create)


@pytest.mark.parametrize(
    "field,value", [("qty", Decimal("30")), ("limitPrice", Decimal("3.125")), ("side", "sell"), ("instrument", "TSLA")]
)
async def test_create_args_differing_from_preview_block_submit(clock, field, value):
    d, create = previewed(clock), CountingCreate(OK)
    with pytest.raises(PreviewMismatch, match="do not match the stored preview"):
        await submit(d, create, clock, args={**CREATE_ARGS, field: value})
    assert_untouched(d, create)


async def test_confirmed_hash_differing_from_preview_blocks_submit(clock):
    d, create = previewed(clock), CountingCreate(OK)
    with pytest.raises(PreviewMismatch, match="confirmed preview hash"):
        await submit(d, create, clock, confirmed=order_args_hash({**ORDER, "qty": Decimal("30")}))
    assert_untouched(d, create)


@pytest.mark.parametrize(
    "age",
    [PREVIEW_MAX_AGE, PREVIEW_MAX_AGE + timedelta(seconds=1), timedelta(hours=2), timedelta(seconds=-5)],
    ids=["exactly-5min", "5min+1s", "2h", "future"],
)
async def test_stale_or_future_preview_blocks_submit(clock, age):
    d, create = previewed(clock, age=age), CountingCreate(OK)
    with pytest.raises(PreviewStale):
        await submit(d, create, clock)
    assert_untouched(d, create)


async def test_preview_just_under_5_minutes_is_accepted(clock):
    d, create = previewed(clock, age=PREVIEW_MAX_AGE - timedelta(seconds=1)), CountingCreate(OK)
    await submit(d, create, clock)
    assert create.calls == 1 and d.status == S.PENDING_APPROVAL


@pytest.mark.parametrize("op", ["modify", "cancel", None])
async def test_non_create_operation_blocks_submit(clock, op):
    d, create = previewed(clock), CountingCreate(OK)
    args = {**ORDER} if op is None else {**ORDER, "operation": op}
    with pytest.raises(OperationNotAllowed):
        await submit(d, create, clock, args=args)
    assert_untouched(d, create)


async def test_sent_args_are_exactly_the_checked_args_and_frozen(clock):
    d, create = previewed(clock), CountingCreate(OK)
    caller_args = dict(CREATE_ARGS)
    await submit(d, create, clock, args=caller_args)
    caller_args["qty"] = Decimal("999")  # mutating the caller's dict after the fact changes nothing
    assert create.sent[0]["qty"] == Decimal("3")
    with pytest.raises(TypeError):
        create.sent[0]["qty"] = Decimal("999")  # type: ignore[index]


def test_all_gate_errors_share_a_base():
    for cls in (TradingDisabled, OperationNotAllowed, PreviewMissing, PreviewMismatch, PreviewStale):
        assert issubclass(cls, SubmissionBlocked)


# --------------------------------------------------------------------------- state machine


def test_terminal_states_have_no_exits():
    assert {S.REJECTED_BY_GUARDRAIL, S.ABANDONED, S.SUCCEEDED, S.FAILED, S.DENIED, S.EXPIRED} == TERMINAL


def test_every_status_has_a_transition_entry():
    assert set(ALLOWED_TRANSITIONS) == set(OrderStatus)


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (S.PREVIEWED, S.PENDING_APPROVAL),
        (S.SUBMIT_FAILED, S.SUBMITTED),
        (S.SUBMIT_FAILED, S.PENDING_APPROVAL),
        (S.SUCCEEDED, S.SUBMITTED),
        (S.INTENT, S.SUBMITTED),
    ],
)
def test_illegal_transitions_raise(clock, frm, to):
    d = OrderDraft(id="x", account_id=ACCOUNT, idempotency_key="k", status=frm)
    with pytest.raises(IllegalTransition):
        d.transition(to, "test", clock())
    assert d.status == frm and d.events == []


# --------------------------------------------------------------------------- submit outcomes (invariant 5)


async def test_success_goes_to_pending_approval_with_one_call(clock):
    create = CountingCreate(OK)
    d = await submit(previewed(clock), create, clock)
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
    d = await submit(previewed(clock), create, clock)
    assert create.calls == 1
    assert d.status == S.SUBMIT_FAILED and d.instruction_id is None


async def test_scope_failure_records_required_scope(clock):
    d = await submit(previewed(clock), CountingCreate(ReauthRequired("x", scope="mcp:brokerage.orders.all")), clock)
    assert d.events[-1].payload["required_scope"] == "mcp:brokerage.orders.all"


@pytest.mark.parametrize("err", [TimeoutError("read timed out"), ConnectionResetError(), ValueError("bad json")])
async def test_ambiguous_failure_moves_to_unknown_without_retry(clock, err):
    create = CountingCreate(err)
    d = await submit(previewed(clock), create, clock)
    assert create.calls == 1 and d.status == S.UNKNOWN


async def test_cancellation_marks_unknown_and_propagates(clock):
    create, d = CountingCreate(asyncio.CancelledError()), previewed(clock)
    with pytest.raises(asyncio.CancelledError):
        await submit(d, create, clock)
    assert create.calls == 1 and d.status == S.UNKNOWN


async def test_cannot_submit_twice(clock):
    d = await submit(previewed(clock), CountingCreate(OK), clock)
    second = CountingCreate(OK)
    with pytest.raises(IllegalTransition):
        await submit(d, second, clock)
    assert second.calls == 0


async def test_cannot_submit_from_submit_failed_directly(clock):
    d = await submit(previewed(clock), CountingCreate(AuthExpired("401")), clock)
    again = CountingCreate(OK)
    with pytest.raises(IllegalTransition):
        await submit(d, again, clock)
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


async def failed(clock) -> OrderDraft:
    return await submit(previewed(clock), CountingCreate(AuthExpired("401")), clock)


def test_skew_tolerance_is_two_minutes():
    assert timedelta(minutes=2) == RESUBMIT_SKEW_TOLERANCE


async def test_resubmission_requires_human(clock):
    d, li = await failed(clock), FakeInstructions()
    with pytest.raises(ResubmissionBlocked, match="human"):
        await authorize_resubmission(d, confirmed_by_human=False, list_instructions=li, clock=clock)
    assert li.calls == 0 and d.status == S.SUBMIT_FAILED


async def test_clean_check_returns_to_previewed_and_requires_fresh_preview(clock):
    d = await failed(clock)
    old = InstructionSummary("old", ACCOUNT, clock() - timedelta(hours=3))
    other_account = InstructionSummary("x", "22222222-2222-2222-2222-222222222222", clock())
    li = FakeInstructions([old, other_account])
    await authorize_resubmission(d, confirmed_by_human=True, list_instructions=li, clock=clock)
    assert li.calls == 1 and d.status == S.PREVIEWED
    create = CountingCreate(OK)
    with pytest.raises(PreviewMissing):  # the old preview was cleared
        await submit(d, create, clock)
    assert create.calls == 0


@pytest.mark.parametrize(
    "questrade_minus_vm",
    [
        timedelta(0),  # clocks agree
        timedelta(seconds=-90),  # Questrade clock 90 s behind the VM
        timedelta(minutes=-2),  # exactly at the tolerance edge (inclusive)
        timedelta(minutes=3),  # Questrade clock ahead: always inside the window
    ],
    ids=["agree", "qt-90s-behind", "qt-2min-behind-edge", "qt-3min-ahead"],
)
async def test_skewed_timestamps_inside_window_block_resubmission(clock, questrade_minus_vm):
    """An instruction that Questrade stamps at (attempt + skew) must still count as 'since the attempt'."""
    d = await failed(clock)
    assert d.submitted_at is not None
    li = FakeInstructions([InstructionSummary("maybe-mine", ACCOUNT, d.submitted_at + questrade_minus_vm)])
    with pytest.raises(ResubmissionBlocked):
        await authorize_resubmission(d, confirmed_by_human=True, list_instructions=li, clock=clock)
    assert d.status == S.UNKNOWN
    assert d.events[-1].payload["instruction_ids"] == ["maybe-mine"]


async def test_instruction_older_than_window_does_not_block(clock):
    d = await failed(clock)
    assert d.submitted_at is not None
    just_outside = d.submitted_at - RESUBMIT_SKEW_TOLERANCE - timedelta(seconds=1)
    li = FakeInstructions([InstructionSummary("earlier-order", ACCOUNT, just_outside)])
    await authorize_resubmission(d, confirmed_by_human=True, list_instructions=li, clock=clock)
    assert d.status == S.PREVIEWED


@pytest.mark.parametrize("via_iso_string", [False, True], ids=["aware-datetime", "parsed-from-iso8601"])
async def test_skewed_timestamp_with_toronto_offset_is_compared_correctly(clock, via_iso_string):
    """Questrade returns ISO 8601 with an ET offset. 90 s after the attempt, stamped in Toronto time,
    must still block, whether given as an aware datetime or parsed from the wire string."""
    d = await failed(clock)
    assert d.submitted_at is not None
    created_et = (d.submitted_at + timedelta(seconds=90)).astimezone(TORONTO)
    if via_iso_string:
        wire = created_et.isoformat()  # e.g. 2026-11-09T10:01:30-05:00
        assert wire.endswith("-05:00")  # 2026-11-09 is after fall back: EST
        created_et = datetime.fromisoformat(wire)
    li = FakeInstructions([InstructionSummary("mine-et", ACCOUNT, created_et)])
    with pytest.raises(ResubmissionBlocked):
        await authorize_resubmission(d, confirmed_by_human=True, list_instructions=li, clock=clock)
    assert d.status == S.UNKNOWN


async def test_instruction_without_timestamp_blocks(clock):
    d = await failed(clock)
    li = FakeInstructions([InstructionSummary("no-ts", ACCOUNT, None)])
    with pytest.raises(ResubmissionBlocked):
        await authorize_resubmission(d, confirmed_by_human=True, list_instructions=li, clock=clock)
    assert d.status == S.UNKNOWN


async def test_list_instructions_failure_keeps_submit_failed(clock):
    d = await failed(clock)
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
