"""Golden scenarios (spec section 9, decisions P7, P8, P11). Fictional data only.

Each scenario's canonical plan is compared byte for byte with ``golden/<name>.json``. Regenerate with
``AURELIUS_UPDATE_GOLDEN=1 uv run pytest tests/unit/ledger/test_golden.py`` and read the diff by eye:
the asserts in each test pin the numbers that matter, so a golden can't drift silently.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

from app.ledger.balances import BalanceEntry, Flow, project_balance, reconcile
from app.ledger.engine import RoomExceeded, allocate, revise
from app.ledger.models import AccountKind, BillFrequency, Goal, RoomStatus
from app.ledger.plan import Bucket, Plan, PlanStatus
from app.ledger.snapshot import plan_bytes
from tests.unit.ledger.factories import D, bill, biweekly, standard_inputs, standard_policy

GOLDEN = Path(__file__).parent / "golden"


def check_golden(name: str, data: bytes) -> None:
    path = GOLDEN / f"{name}.json"
    pretty = json.dumps(json.loads(data), indent=2, sort_keys=True) + "\n"
    if os.environ.get("AURELIUS_UPDATE_GOLDEN") == "1":
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(pretty, encoding="utf-8", newline="\n")
    assert path.exists(), f"missing golden {path.name}; run with AURELIUS_UPDATE_GOLDEN=1"
    assert path.read_text(encoding="utf-8") == pretty, f"{name} differs from its golden"


def amounts(plan: Plan) -> dict[str, str]:
    return {line.line_id: str(line.amount) for line in plan.lines}


def notice_codes(plan: Plan) -> list[str]:
    return [n.code for n in plan.notices]


def test_ordinary_month() -> None:
    plan = allocate(standard_inputs(), standard_policy())
    check_golden("ordinary_month", plan_bytes(plan))
    a = amounts(plan)
    assert plan.status is PlanStatus.OK
    assert a["bill:rent:2027-01-15"] == "900.00"
    assert a["bill:phone:2027-01-20"] == "45.00"
    assert a["bill:insurance:2027-06-01"] == "54.54"  # 600 / 11 paydays, rounded down
    assert a["essential:groceries"] == "200.00"  # 400 / 2 January paydays
    assert a["essential:transit"] == "60.00"
    assert a["buffer:chq"] == "200.00"  # 500 target - 300 projected
    assert a["emergency_fund:hisa"] == "200.00"  # 10% cap of 2000
    assert a["goal:trip"] == "92.30"  # 1200 / 13 paydays to 2027-06-30, rounded down
    assert a["investing:tfsa"] == "150.00"  # room
    assert a["investing:fhsa"] == "98.16"  # all that's left after TFSA
    assert a["investing:nonreg"] == "0.00"
    assert a["explore:explore"] == "0.00"  # nothing left
    assert a["spending"] == "0.00"
    assert plan.total() == D("2000.00")


def test_shortfall_month() -> None:
    plan = allocate(standard_inputs(net_pay=D("700.00")), standard_policy())
    check_golden("shortfall_month", plan_bytes(plan))
    a = amounts(plan)
    assert plan.status is PlanStatus.SHORTFALL
    assert a["bill:rent:2027-01-15"] == "700.00"  # critical (autopay) first, partial
    assert a["bill:phone:2027-01-20"] == "0.00"
    assert all(line.amount == 0 for line in plan.lines if line.bucket is not Bucket.BILLS)
    uncovered = {dict(n.params)["bill_id"]: dict(n.params)["short"] for n in plan.notices if n.code == "bill_uncovered"}
    assert uncovered == {"rent": "200.00", "phone": "45.00", "insurance": "54.54"}


def test_shortfall_autopay_due_later_beats_manual_due_earlier() -> None:
    inputs = standard_inputs(
        net_pay=D("500.00"),
        bills=(
            bill("utilities", "400.00", date(2027, 1, 10)),  # manual, due first
            bill("loan", "300.00", date(2027, 1, 20), autopay=True),  # autopay, due later
        ),
    )
    plan = allocate(inputs, standard_policy())
    check_golden("shortfall_autopay_first", plan_bytes(plan))
    a = amounts(plan)
    assert plan.status is PlanStatus.SHORTFALL
    assert a["bill:loan:2027-01-20"] == "300.00"
    assert a["bill:utilities:2027-01-10"] == "200.00"  # first that doesn't fit: partial
    assert [line.line_id for line in plan.lines if line.bucket is Bucket.BILLS] == [
        "bill:loan:2027-01-20",
        "bill:utilities:2027-01-10",
    ]


def test_three_paycheque_month() -> None:
    inputs = standard_inputs(payday=date(2027, 1, 1), schedule=biweekly(date(2027, 1, 1)))
    plan = allocate(inputs, standard_policy())
    check_golden("three_paycheque_month", plan_bytes(plan))
    a = amounts(plan)
    assert a["essential:groceries"] == "133.33"  # 400 / 3, rounded down
    assert dict(plan.line("essential:groceries").reason.params)["paydays_in_month"] == "3"


def test_annual_bill_sinking_fund_progresses() -> None:
    insurance = bill("insurance", "600.00", date(2027, 6, 1), frequency=BillFrequency.ANNUAL, sinking=True)
    first = allocate(standard_inputs(bills=(insurance,)), standard_policy())
    check_golden("sinking_fund", plan_bytes(first))
    assert amounts(first)["bill:insurance:2027-06-01"] == "54.54"
    # Last paycheque before the due date (2027-05-28): only the unfunded part, whatever was set aside.
    late = insurance.model_copy(update={"sinking_funded": D("545.40")})
    last = allocate(standard_inputs(payday=date(2027, 5, 28), bills=(late,)), standard_policy())
    assert amounts(last)["bill:insurance:2027-06-01"] == "54.60"


def test_sinking_fund_due_on_the_payday_itself_takes_the_full_unfunded_amount() -> None:
    insurance = bill(
        "insurance",
        "600.00",
        date(2027, 1, 8),
        frequency=BillFrequency.ANNUAL,
        sinking=True,
        sinking_funded=D("100.00"),
    )
    plan = allocate(standard_inputs(bills=(insurance,)), standard_policy())
    line = plan.line("bill:insurance:2027-01-08")
    assert line.amount == D("500.00")
    assert dict(line.reason.params)["paydays_remaining"] == "0"


def test_january_with_stale_contribution_room() -> None:
    stale = (RoomStatus(kind=AccountKind.TFSA, year=2026, remaining=D("5000.00")),)
    plan = allocate(standard_inputs(room=stale), standard_policy())
    check_golden("january_stale_room", plan_bytes(plan))
    a = amounts(plan)
    assert a["investing:tfsa"] == "0.00"
    assert a["investing:fhsa"] == "0.00"
    assert a["investing:nonreg"] == "248.16"
    stale_kinds = sorted(dict(n.params)["kind"] for n in plan.notices if n.code == "room_stale")
    assert stale_kinds == ["fhsa", "tfsa"]


def test_goal_past_its_deadline_is_flagged_and_still_funded() -> None:
    goals = (
        Goal(
            id="laptop",
            name="Laptop",
            target=D("1500.00"),
            target_date=date(2026, 12, 1),
            account_id="hisa",
            priority=1,
            saved=D("1400.00"),
        ),
    )
    plan = allocate(standard_inputs(goals=goals), standard_policy())
    check_golden("goal_overdue", plan_bytes(plan))
    assert amounts(plan)["goal:laptop"] == "100.00"
    assert "goal_overdue" in notice_codes(plan)


def test_revision_with_a_line_override() -> None:
    inputs, policy = standard_inputs(), standard_policy()
    plan = allocate(inputs, policy)
    revised = revise(plan, inputs, policy, {"investing:fhsa": D("0.00"), "goal:trip": D("150.00")})
    check_golden("revision_override", plan_bytes(revised))
    a = amounts(revised)
    assert a["investing:fhsa"] == "0.00"
    assert a["goal:trip"] == "150.00"
    assert a["spending"] == "40.46"  # 98.16 freed - 57.70 extra
    assert revised.line("goal:trip").overridden_from == D("92.30")
    assert revised.total() == D("2000.00")
    with pytest.raises(RoomExceeded):
        revise(plan, inputs, policy, {"investing:tfsa": D("151.00"), "investing:fhsa": D("0.00")})


def test_actual_pay_differs_from_expected() -> None:
    plan = allocate(standard_inputs(net_pay=D("1850.00"), expected_pay=D("2000.00")), standard_policy())
    check_golden("pay_differs", plan_bytes(plan))
    notice = next(n for n in plan.notices if n.code == "pay_differs_from_expected")
    assert dict(notice.params) == {"actual": "1850.00", "difference": "-150.00", "expected": "2000.00"}
    assert plan.total() == D("1850.00")


def test_reconciliation_drift_after_an_unexecuted_transfer() -> None:
    """P8: a plan approved a 200 transfer to the HISA, Rudra never made it, then a balance entry arrives."""
    opening = BalanceEntry(account_id="hisa", amount=D("1000.00"), as_of=date(2027, 1, 1))
    approved = [Flow(account_id="hisa", on=date(2027, 1, 8), amount=D("200.00"))]
    assert project_balance("hisa", opening, approved) == D("1200.00")
    entry = BalanceEntry(account_id="hisa", amount=D("1000.00"), as_of=date(2027, 1, 20))
    drift = reconcile(opening, approved, entry)
    assert (drift.projected, drift.actual, drift.difference) == (D("1200.00"), D("1000.00"), D("-200.00"))
    # From then on, projection starts at the actual balance.
    later = [*approved, Flow(account_id="hisa", on=date(2027, 1, 22), amount=D("50.00"))]
    assert project_balance("hisa", entry, later) == D("1050.00")
    check_golden("reconciliation_drift", drift.model_dump_json().encode())


def test_investing_ahead_of_an_underfunded_emergency_fund_gets_a_notice() -> None:
    """P18: non-blocking. The plan is still computed in the policy's order."""
    from app.ledger.policy import MiddleBucket

    order = (
        MiddleBucket.INVESTING,
        MiddleBucket.EXPLORE,
        MiddleBucket.EMERGENCY_FUND,
        MiddleBucket.BUFFER,
        MiddleBucket.GOALS,
    )
    plan = allocate(standard_inputs(), standard_policy(order=order))
    check_golden("order_warning", plan_bytes(plan))
    flagged = sorted(
        dict(n.params)["bucket"] for n in plan.notices if n.code == "ordered_ahead_of_underfunded_emergency_fund"
    )
    assert flagged == ["explore", "investing"]
    assert plan.status is PlanStatus.OK
    # Default order, or an emergency fund at target: no notice.
    assert "ordered_ahead_of_underfunded_emergency_fund" not in notice_codes(
        allocate(standard_inputs(), standard_policy())
    )
    from tests.unit.ledger.factories import account, standard_accounts

    funded = tuple(
        a if a.id != "hisa" else account("hisa", AccountKind.HISA, "99999.00", "emergency_fund")
        for a in standard_accounts()
    )
    plan = allocate(standard_inputs(accounts=funded), standard_policy(order=order))
    assert "ordered_ahead_of_underfunded_emergency_fund" not in notice_codes(plan)
