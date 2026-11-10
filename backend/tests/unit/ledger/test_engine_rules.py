"""Focused rule tests: sinking guard (P7), money validation, typed errors, immutability."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.ledger.engine import (
    ConservationError,
    InputError,
    PolicyError,
    RevisionError,
    allocate,
    revise,
    sinking_per_pay,
)
from app.ledger.models import Account, AccountKind, Bill, BillFrequency, LedgerInputs, RoomStatus
from app.ledger.money import cents, floor_cents, percent_of
from app.ledger.policy import FixedCap, InvestingPolicy, SurplusPolicy
from app.ledger.snapshot import load_snapshot, snapshot
from tests.unit.ledger.factories import D, account, standard_accounts, standard_inputs, standard_policy


@pytest.mark.parametrize(
    ("unfunded", "paydays", "expected"),
    [
        ("600.00", 0, "600.00"),
        ("600.00", -1, "600.00"),
        ("600.00", 1, "600.00"),
        ("600.00", 11, "54.54"),
        ("0.00", 0, "0.00"),
        ("0.05", 3, "0.01"),
        ("0.02", 3, "0.00"),
    ],
)
def test_sinking_per_pay_guard_and_rounding(unfunded: str, paydays: int, expected: str) -> None:
    assert sinking_per_pay(D(unfunded), paydays) == D(expected)


def test_rounding_is_down() -> None:
    assert floor_cents(D("1.239")) == D("1.23")
    assert percent_of(D("33.333"), D("100.00")) == D("33.33")


@pytest.mark.parametrize("bad", [1.5, 1, "1.00", True])
def test_cents_rejects_non_decimal(bad: object) -> None:
    with pytest.raises(TypeError):
        cents(bad)  # type: ignore[arg-type]


def test_cents_rejects_sub_cent_and_non_finite() -> None:
    for bad in (D("1.001"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            cents(bad)


def test_models_reject_floats_and_sub_cent_amounts() -> None:
    with pytest.raises(ValidationError):
        standard_inputs(net_pay=2000.0)
    with pytest.raises(ValidationError):
        standard_inputs(net_pay=D("2000.001"))
    with pytest.raises(ValidationError):
        Bill(id="x", name="x", amount=12.5, frequency=BillFrequency.MONTHLY, anchor=date(2027, 1, 1))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):  # signed Money (balances, room) is strict too
        Account(id="a", kind=AccountKind.HISA, currency="CAD", projected_balance=-10.5)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RoomStatus(kind=AccountKind.TFSA, year=2027, remaining=100)  # type: ignore[arg-type]


def test_snapshot_refuses_floats() -> None:
    raw = snapshot(standard_inputs(), standard_policy()).replace(b'"net_pay":"2000.00"', b'"net_pay":2000.0')
    with pytest.raises(ValueError, match="float"):
        load_snapshot(raw)


def test_plan_is_frozen() -> None:
    plan = allocate(standard_inputs(), standard_policy())
    with pytest.raises(ValidationError):
        plan.status = plan.status  # type: ignore[misc]
    with pytest.raises(ValidationError):
        plan.lines[0].amount = D("1.00")  # type: ignore[misc]


def test_usd_accounts_are_never_targets() -> None:
    accounts = (*standard_accounts(), account("usd", AccountKind.NON_REGISTERED, currency="USD"))
    policy = standard_policy(
        investing=InvestingPolicy(per_pay_limit=FixedCap(kind="fixed", amount=D("10.00")), targets=("usd",))
    )
    with pytest.raises(PolicyError, match="USD"):
        allocate(standard_inputs(accounts=accounts), policy)


def test_two_buffer_accounts_is_an_input_error() -> None:
    accounts = (*standard_accounts(), account("chq2", AccountKind.CHEQUING, "0.00", "buffer"))
    with pytest.raises(InputError, match="buffer"):
        allocate(standard_inputs(accounts=accounts), standard_policy())


def test_surplus_redirect() -> None:
    policy = standard_policy(
        investing=None,
        explore=None,
        surplus=SurplusPolicy(spending_cap=FixedCap(kind="fixed", amount=D("100.00")), redirect_to="nonreg"),
    )
    plan = allocate(standard_inputs(), policy)
    assert plan.line("spending").amount == D("100.00")
    assert plan.line("surplus:nonreg").amount > 0
    assert plan.total() == D("2000.00")
    with pytest.raises(PolicyError, match="registered"):
        allocate(
            standard_inputs(),
            standard_policy(
                surplus=SurplusPolicy(spending_cap=FixedCap(kind="fixed", amount=D("0.00")), redirect_to="tfsa")
            ),
        )


def test_revision_rejections() -> None:
    inputs, policy = standard_inputs(), standard_policy()
    plan = allocate(inputs, policy)
    with pytest.raises(ConservationError):
        revise(plan, inputs, policy, {"goal:trip": D("5000.00")})
    with pytest.raises(RevisionError, match="balancing"):
        revise(plan, inputs, policy, {"spending": D("1.00")})
    with pytest.raises(RevisionError, match="unknown"):
        revise(plan, inputs, policy, {"goal:nope": D("1.00")})
    with pytest.raises(RevisionError, match="cents"):
        revise(plan, inputs, policy, {"goal:trip": D("1.005")})
    other: LedgerInputs = standard_inputs(net_pay=D("1999.00"))
    with pytest.raises(RevisionError, match="inputs"):
        revise(plan, other, policy, {"goal:trip": D("1.00")})


def test_same_inputs_give_identical_bytes() -> None:
    from app.ledger.snapshot import plan_bytes

    assert plan_bytes(allocate(standard_inputs(), standard_policy())) == plan_bytes(
        allocate(standard_inputs(), standard_policy())
    )
