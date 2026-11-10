"""Property tests on the engine (spec section 9). Inputs are random and fictional."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.ledger.engine import ConservationError, RevisionError, RoomExceeded, allocate, revise
from app.ledger.models import (
    REGISTERED,
    Account,
    AccountKind,
    Bill,
    BillFrequency,
    BudgetLine,
    BudgetPeriod,
    Goal,
    IncomeSchedule,
    LedgerInputs,
    PayFrequency,
    RoomStatus,
)
from app.ledger.plan import Bucket, Plan, PlanStatus
from app.ledger.policy import (
    DEFAULT_ORDER,
    AllocationPolicy,
    BufferPolicy,
    EmergencyFundPolicy,
    ExplorePolicy,
    FixedCap,
    GoalsPolicy,
    InvestingPolicy,
    PercentCap,
    cap_amount,
)
from app.ledger.snapshot import load_snapshot, plan_bytes, snapshot

PROFILE = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

cents = st.integers(min_value=0, max_value=500_000).map(lambda c: Decimal(c).scaleb(-2))
positive_cents = st.integers(min_value=1, max_value=300_000).map(lambda c: Decimal(c).scaleb(-2))
percent = st.integers(min_value=0, max_value=10_000).map(lambda p: Decimal(p).scaleb(-2))
day = st.dates(min_value=date(2026, 1, 1), max_value=date(2029, 12, 31))


@st.composite
def caps(draw: st.DrawFn) -> FixedCap | PercentCap:
    if draw(st.booleans()):
        return FixedCap(kind="fixed", amount=draw(cents))
    return PercentCap(kind="percent", percent=draw(percent))


@st.composite
def bills(draw: st.DrawFn, payday: date) -> tuple[Bill, ...]:
    out = []
    for i in range(draw(st.integers(0, 5))):
        freq = draw(st.sampled_from(list(BillFrequency)))
        anchor = payday + timedelta(days=draw(st.integers(-40, 200)))
        sinking = freq in (BillFrequency.ANNUAL, BillFrequency.EVERY_N_MONTHS) and draw(st.booleans())
        amount = draw(positive_cents)
        out.append(
            Bill(
                id=f"b{i}",
                name=f"Bill {i}",
                amount=amount,
                frequency=freq,
                anchor=anchor,
                day_of_month=draw(st.integers(1, 31)) if freq is not BillFrequency.BIWEEKLY else None,
                interval_months=draw(st.integers(1, 6)) if freq is BillFrequency.EVERY_N_MONTHS else None,
                autopay=draw(st.booleans()),
                critical=draw(st.booleans()),
                essential=draw(st.booleans()),
                sinking=sinking,
                sinking_funded=min(draw(cents), amount) if sinking else Decimal("0.00"),
                pay_from_account_id=draw(st.sampled_from([None, "chq"])),
            )
        )
    return tuple(out)


ACCOUNT_IDS = ("chq", "hisa", "tfsa", "fhsa", "rrsp", "nonreg", "usd")


@st.composite
def ledger(draw: st.DrawFn) -> tuple[LedgerInputs, AllocationPolicy]:
    payday = draw(day)
    freq = draw(st.sampled_from(list(PayFrequency)))
    schedule = IncomeSchedule(frequency=freq, anchor=payday)
    accounts = (
        Account(id="chq", kind=AccountKind.CHEQUING, currency="CAD", roles=("buffer",), projected_balance=draw(cents)),
        Account(
            id="hisa", kind=AccountKind.HISA, currency="CAD", roles=("emergency_fund",), projected_balance=draw(cents)
        ),
        Account(id="tfsa", kind=AccountKind.TFSA, currency="CAD", projected_balance=draw(cents)),
        Account(id="fhsa", kind=AccountKind.FHSA, currency="CAD", projected_balance=draw(cents)),
        Account(id="rrsp", kind=AccountKind.RRSP, currency="CAD", projected_balance=draw(cents)),
        Account(id="nonreg", kind=AccountKind.NON_REGISTERED, currency="CAD", projected_balance=draw(cents)),
        Account(id="usd", kind=AccountKind.NON_REGISTERED, currency="USD", projected_balance=draw(cents)),
    )
    room = tuple(
        RoomStatus(
            kind=kind,
            year=payday.year if draw(st.integers(0, 3)) else payday.year - 1,  # sometimes stale
            remaining=draw(st.integers(-50_000, 800_000).map(lambda c: Decimal(c).scaleb(-2))),
        )
        for kind in sorted(REGISTERED)
        if draw(st.booleans())
    )
    goal_accounts = st.sampled_from(["hisa", "tfsa", "fhsa", "rrsp", "nonreg", "usd"])
    goals = tuple(
        Goal(
            id=f"g{i}",
            name=f"Goal {i}",
            target=draw(positive_cents),
            target_date=draw(st.none() | day),
            account_id=draw(goal_accounts),
            priority=draw(st.integers(0, 5)),
            saved=draw(cents),
        )
        for i in range(draw(st.integers(0, 4)))
    )
    budget = tuple(
        BudgetLine(
            id=f"e{i}", name=f"Essential {i}", amount=draw(cents), period=draw(st.sampled_from(list(BudgetPeriod)))
        )
        for i in range(draw(st.integers(0, 3)))
    )
    inputs = LedgerInputs(
        payday=payday,
        net_pay=draw(cents),
        expected_pay=draw(st.none() | cents),
        schedule=schedule,
        bills=draw(bills(payday)),
        budget_lines=budget,
        accounts=accounts,
        goals=goals,
        room=room,
    )
    targets = draw(st.lists(st.sampled_from(["tfsa", "fhsa", "rrsp", "nonreg"]), min_size=1, max_size=4, unique=True))
    policy = AllocationPolicy(
        version=draw(st.integers(1, 9)),
        order=tuple(draw(st.permutations(list(DEFAULT_ORDER)))),
        buffer=draw(st.none() | st.builds(BufferPolicy, target=cents)),
        emergency_fund=draw(
            st.none()
            | st.builds(
                EmergencyFundPolicy,
                months=st.integers(1, 12).map(Decimal),
                per_pay_cap=caps(),
            )
        ),
        goals=GoalsPolicy(undated_per_pay=draw(cents), overdue_per_pay_cap=draw(st.none() | caps())),
        investing=draw(st.none() | st.builds(InvestingPolicy, per_pay_limit=caps(), targets=st.just(tuple(targets)))),
        explore=draw(st.none() | st.builds(ExplorePolicy, percent=percent, account_id=st.just("nonreg"))),
    )
    return inputs, policy


def registered_totals(plan: Plan, inputs: LedgerInputs) -> dict[AccountKind, Decimal]:
    kinds = {a.id: a.kind for a in inputs.accounts}
    out = {kind: Decimal("0.00") for kind in REGISTERED}
    for line in plan.lines:
        if line.account_id is not None and kinds[line.account_id] in REGISTERED:
            out[kinds[line.account_id]] += line.amount
    return out


def allowed_room(inputs: LedgerInputs, kind: AccountKind) -> Decimal:
    status = next((r for r in inputs.room if r.kind is kind and r.year == inputs.payday.year), None)
    return max(status.remaining, Decimal("0.00")) if status is not None else Decimal("0.00")


@PROFILE
@given(ledger())
def test_conservation_and_no_negative_lines(case: tuple[LedgerInputs, AllocationPolicy]) -> None:
    inputs, policy = case
    plan = allocate(inputs, policy)
    assert plan.total() == inputs.net_pay
    assert all(line.amount >= 0 for line in plan.lines)
    assert all(line.amount == line.amount.quantize(Decimal("0.01")) for line in plan.lines)


@PROFILE
@given(ledger())
def test_registered_accounts_never_exceed_room(case: tuple[LedgerInputs, AllocationPolicy]) -> None:
    inputs, policy = case
    plan = allocate(inputs, policy)
    for kind, total in registered_totals(plan, inputs).items():
        assert total <= allowed_room(inputs, kind), kind


@PROFILE
@given(ledger())
def test_no_bucket_exceeds_its_cap_or_target(case: tuple[LedgerInputs, AllocationPolicy]) -> None:
    inputs, policy = case
    plan = allocate(inputs, policy)
    accounts = {a.id: a for a in inputs.accounts}
    for line in plan.lines:
        values = dict(line.reason.params)
        if line.bucket is Bucket.BUFFER:
            assert line.amount <= max(Decimal(values["target"]) - Decimal(values["projected_balance"]), Decimal(0))
        elif line.bucket is Bucket.EMERGENCY_FUND:
            assert line.amount <= Decimal(values["per_pay_cap"])
            assert line.amount <= max(Decimal(values["target"]) - Decimal(values["projected_balance"]), Decimal(0))
        elif line.bucket is Bucket.GOALS:
            assert line.amount <= Decimal(values["outstanding"])
        elif line.bucket is Bucket.EXPLORE:
            assert policy.explore is not None
            assert line.amount <= cap_amount(PercentCap(kind="percent", percent=policy.explore.percent), inputs.net_pay)
        elif line.bucket is Bucket.BILLS and line.reason.code == "bill_due_in_window":
            assert line.amount <= accounts_bill_amount(inputs, values["bill_id"])
    if policy.investing is not None:
        invested = sum((line.amount for line in plan.lines if line.bucket is Bucket.INVESTING), Decimal(0))
        assert invested <= cap_amount(policy.investing.per_pay_limit, inputs.net_pay)
    assert accounts  # used


def accounts_bill_amount(inputs: LedgerInputs, bill_id: str) -> Decimal:
    return next(b.amount for b in inputs.bills if b.id == bill_id)


@PROFILE
@given(ledger())
def test_shortfall_funds_nothing_below_bills(case: tuple[LedgerInputs, AllocationPolicy]) -> None:
    inputs, policy = case
    plan = allocate(inputs, policy)
    if plan.status is PlanStatus.SHORTFALL:
        assert all(line.amount == 0 for line in plan.lines if line.bucket is not Bucket.BILLS)
        assert any(n.code == "bill_uncovered" for n in plan.notices)


def percent_caps(policy: AllocationPolicy) -> int:
    """How many caps in the policy are percentages (each rounds down independently)."""
    caps_in_use: list[object] = []
    if policy.emergency_fund is not None:
        caps_in_use.append(policy.emergency_fund.per_pay_cap)
    if policy.investing is not None:
        caps_in_use.append(policy.investing.per_pay_limit)
    if policy.goals.overdue_per_pay_cap is not None:
        caps_in_use.append(policy.goals.overdue_per_pay_cap)
    if policy.surplus is not None:
        caps_in_use.append(policy.surplus.spending_cap)
    return sum(isinstance(c, PercentCap) for c in caps_in_use) + (policy.explore is not None)


@PROFILE
@given(ledger(), st.integers(min_value=1, max_value=200_000).map(lambda c: Decimal(c).scaleb(-2)))
def test_more_pay_never_reduces_any_bucket_beyond_rounding(
    case: tuple[LedgerInputs, AllocationPolicy], extra: Decimal
) -> None:
    """Priority monotonicity, up to one cent per percentage cap (pending Rudra's decision, see below)."""
    inputs, policy = case
    richer = inputs.model_copy(update={"net_pay": inputs.net_pay + extra})
    before = {line.line_id: line.amount for line in allocate(inputs, policy).lines}
    after = {line.line_id: line.amount for line in allocate(richer, policy).lines}
    assert before.keys() == after.keys()
    tolerance = Decimal(percent_caps(policy)).scaleb(-2)
    shrunk = {k: (before[k], after[k]) for k in before if after[k] < before[k] - tolerance}
    assert not shrunk, shrunk


@pytest.mark.xfail(strict=True, reason="OPEN DECISION: independent round-down of two percentage caps")
def test_strict_monotonicity_counterexample() -> None:
    """Net 0.09 -> 0.10 with a 10% emergency-fund cap and a 20% explore cap: both round up a cent at
    once, so investing (below them) drops 0.08 -> 0.07. Strict "more pay never reduces" can't hold
    while percentage caps round down independently. Kept as a strict xfail until Rudra decides."""
    from app.ledger.policy import MiddleBucket
    from tests.unit.ledger.factories import account, standard_accounts, standard_inputs, standard_policy

    policy = standard_policy(
        order=(
            MiddleBucket.EMERGENCY_FUND,
            MiddleBucket.EXPLORE,
            MiddleBucket.INVESTING,
            MiddleBucket.BUFFER,
            MiddleBucket.GOALS,
        ),
        buffer=None,
        goals=GoalsPolicy(),
        emergency_fund=EmergencyFundPolicy(
            months=Decimal(1), per_pay_cap=PercentCap(kind="percent", percent=Decimal(10))
        ),
        explore=ExplorePolicy(percent=Decimal(20), account_id="explore"),
        investing=InvestingPolicy(per_pay_limit=FixedCap(kind="fixed", amount=Decimal("1000.00")), targets=("nonreg",)),
    )
    accounts = tuple(
        a if a.id != "hisa" else account("hisa", AccountKind.HISA, "-9999.00", "emergency_fund")
        for a in standard_accounts()
    )
    base = standard_inputs(bills=(), budget_lines=(), goals=(), accounts=accounts)
    low = allocate(base.model_copy(update={"net_pay": Decimal("0.09")}), policy).line("investing:nonreg").amount
    high = allocate(base.model_copy(update={"net_pay": Decimal("0.10")}), policy).line("investing:nonreg").amount
    assert high >= low


@PROFILE
@given(ledger())
def test_recomputing_from_the_snapshot_reproduces_the_plan(case: tuple[LedgerInputs, AllocationPolicy]) -> None:
    inputs, policy = case
    plan = allocate(inputs, policy)
    again_inputs, again_policy = load_snapshot(snapshot(inputs, policy))
    assert plan_bytes(allocate(again_inputs, again_policy)) == plan_bytes(plan)


@PROFILE
@given(ledger(), st.data())
def test_revisions_conserve_or_are_rejected(case: tuple[LedgerInputs, AllocationPolicy], data: st.DataObject) -> None:
    inputs, policy = case
    plan = allocate(inputs, policy)
    editable = [line.line_id for line in plan.lines if line.line_id != "spending"]
    if not editable:
        return
    chosen = data.draw(st.lists(st.sampled_from(editable), min_size=1, max_size=3, unique=True))
    overrides = {line_id: data.draw(cents) for line_id in chosen}
    try:
        revised = revise(plan, inputs, policy, overrides)
    except (ConservationError, RoomExceeded):
        return
    except RevisionError as exc:  # pragma: no cover - only typed errors above are expected
        raise AssertionError(exc) from exc
    assert revised.total() == inputs.net_pay
    for kind, total in registered_totals(revised, inputs).items():
        assert total <= allowed_room(inputs, kind)
