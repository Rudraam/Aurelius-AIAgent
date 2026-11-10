"""The payday waterfall: ``allocate(inputs, policy) -> Plan``. Pure (invariant 2).

Buckets, in order (spec section 4, decisions P4-P14):

1. **Bills** due in [payday, next payday), plus sinking-fund amounts. In a shortfall: critical bills
   first (autopay or flagged), then the rest, each group by due date. The first that doesn't fit gets
   a partial line, and every uncovered amount becomes a ``bill_uncovered`` notice (P11).
2. **Essentials**: budget lines for this pay (per-month lines ÷ paydays in the payday's month, P5).
3. to 7. **Buffer, emergency fund, goals, investing, explore**, in the policy's order.
8. **Spending**: everything left, rounding remainder included. With a surplus policy, spending is
   capped and the excess goes to the redirect account.

Every computed amount is rounded down to the cent (P4), so the lines always sum to net pay exactly
(invariant 1). Every line into a TFSA, FHSA or RRSP account, from any bucket, is capped by the
remaining room for the payday's calendar year. Missing or stale room allows nothing (invariant 7).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, localcontext

from app.ledger.models import (
    REGISTERED,
    Account,
    AccountKind,
    Bill,
    BillFrequency,
    BudgetPeriod,
    Goal,
    LedgerInputs,
)
from app.ledger.money import ZERO, cents, divide, floor_cents, ledger_context, multiply, percent_of
from app.ledger.plan import SPENDING_LINE_ID, Bucket, Line, Notice, Plan, PlanStatus, Reason, params
from app.ledger.policy import AllocationPolicy, MiddleBucket, cap_amount
from app.ledger.schedule import (
    PAYDAYS_PER_YEAR,
    bill_due_dates,
    next_due_on_or_after,
    next_payday_after,
    paydays,
    paydays_in_month,
)
from app.ledger.snapshot import snapshot_hash

_DAY = timedelta(days=1)


class LedgerError(Exception):
    """Base for typed ledger errors."""


class PolicyError(LedgerError):
    """The policy refers to an account that can't be used that way."""


class InputError(LedgerError):
    """The inputs are inconsistent (e.g. two buffer accounts)."""


class RevisionError(LedgerError):
    """A revision was rejected."""


class ConservationError(RevisionError):
    """The overrides exceed net pay: spending would go negative."""


class RoomExceeded(RevisionError):
    """The overrides put more into a registered account type than its remaining room."""


# ---------------------------------------------------------------------------------------------------
# Pure helpers (exported for direct tests)


def sinking_per_pay(unfunded: Decimal, paydays_remaining: int) -> Decimal:
    """P7: unfunded ÷ paydays remaining, rounded down. With none remaining, the whole unfunded amount."""
    if unfunded <= 0:
        return ZERO
    if paydays_remaining <= 0:
        return unfunded
    return floor_cents(divide(unfunded, Decimal(paydays_remaining)))


def monthly_equivalent_bill(bill: Bill) -> Decimal:
    """P6 monthly equivalent, at full precision."""
    if bill.frequency is BillFrequency.MONTHLY:
        return bill.amount
    if bill.frequency is BillFrequency.BIWEEKLY:
        return divide(multiply(bill.amount, Decimal(26)), Decimal(12))
    if bill.frequency is BillFrequency.ANNUAL:
        return divide(bill.amount, Decimal(12))
    assert bill.interval_months is not None
    return divide(bill.amount, Decimal(bill.interval_months))


def monthly_essentials(inputs: LedgerInputs) -> Decimal:
    """P6: essential bills plus all budget lines, as a monthly figure at full precision."""
    per_year = Decimal(PAYDAYS_PER_YEAR[inputs.schedule.frequency])
    total = ZERO
    with localcontext(ledger_context()):
        for bill in inputs.bills:
            if bill.active and bill.essential:
                total += monthly_equivalent_bill(bill)
        for line in inputs.budget_lines:
            if line.period is BudgetPeriod.PER_MONTH:
                total += line.amount
            else:
                total += divide(multiply(line.amount, per_year), Decimal(12))
    return total


# ---------------------------------------------------------------------------------------------------


@dataclass
class _Allocator:
    inputs: LedgerInputs
    policy: AllocationPolicy
    remaining: Decimal = ZERO
    lines: list[Line] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    status: PlanStatus = PlanStatus.OK
    room_left: dict[AccountKind, Decimal | None] = field(default_factory=dict)
    _stale_noticed: set[AccountKind] = field(default_factory=set)

    def __post_init__(self) -> None:
        inputs = self.inputs
        self.remaining = inputs.net_pay
        self.accounts: dict[str, Account] = {a.id: a for a in inputs.accounts}
        self.next_payday = next_payday_after(inputs.schedule, inputs.bank_holidays, inputs.payday)
        year = inputs.payday.year
        for kind in sorted(REGISTERED):
            status = next((r for r in inputs.room if r.kind is kind and r.year == year), None)
            if status is None:
                self.room_left[kind] = None
            else:
                if status.remaining < 0:
                    self._notice("room_over_contributed", kind=kind.value, year=year, remaining=status.remaining)
                self.room_left[kind] = max(status.remaining, ZERO)

    # -- plumbing ---------------------------------------------------------------------------------

    def _notice(self, code: str, **values: object) -> None:
        self.notices.append(Notice(code=code, params=params(values)))

    def _account(self, account_id: str, *, purpose: str) -> Account:
        account = self.accounts.get(account_id)
        if account is None:
            raise InputError(f"{purpose}: unknown account {account_id!r}")
        if account.currency != "CAD":
            raise PolicyError(f"{purpose}: account {account_id!r} is {account.currency}; only CAD is allocated (P14)")
        return account

    def _role_account(self, role: str) -> Account | None:
        found = [a for a in self.inputs.accounts if role in a.roles]
        if len(found) > 1:
            raise InputError(f"more than one account has role {role!r}")
        if not found:
            return None
        return self._account(found[0].id, purpose=f"{role} account")

    def _room_cap(self, account: Account | None) -> Decimal | None:
        """None: no room limit. Otherwise the room left for this account's type."""
        if account is None or not account.registered:
            return None
        left = self.room_left[account.kind]
        if left is None:
            if account.kind not in self._stale_noticed:
                self._stale_noticed.add(account.kind)
                self._notice("room_stale", kind=account.kind.value, year=self.inputs.payday.year)
            return ZERO
        return left

    def _give(
        self,
        line_id: str,
        bucket: Bucket,
        account: Account | None,
        want: Decimal,
        reason: str,
        **reason_params: object,
    ) -> Decimal:
        """Allocate up to ``want`` (capped by what's left and by room) and record the line."""
        amount = min(max(want, ZERO), self.remaining)
        room = self._room_cap(account)
        if room is not None:
            amount = min(amount, room)
            if account is not None:
                self.room_left[account.kind] = room - amount
        amount = cents(amount)
        self.remaining -= amount
        self.lines.append(
            Line(
                line_id=line_id,
                bucket=bucket,
                account_id=account.id if account is not None else None,
                amount=amount,
                reason=Reason(code=reason, params=params(reason_params)),
            )
        )
        return amount

    def _paydays_before(self, deadline: date, *, inclusive: bool) -> int:
        """Paydays from this one up to ``deadline``, this one included if it is before the deadline."""
        end = deadline + _DAY if inclusive else deadline
        current = 1 if self.inputs.payday < end else 0
        future = paydays(self.inputs.schedule, self.inputs.bank_holidays, self.next_payday, end)
        return current + len(future)

    # -- buckets ----------------------------------------------------------------------------------

    def bills(self) -> None:
        inputs = self.inputs
        items: list[tuple[bool, date, str, str, Bill, Decimal, str, dict[str, object]]] = []
        for bill in sorted(inputs.bills, key=lambda b: b.id):
            if not bill.active:
                continue
            if bill.sinking:
                due = next_due_on_or_after(bill, inputs.payday)
                unfunded = max(bill.amount - bill.sinking_funded, ZERO)
                remaining_paydays = self._paydays_before(due, inclusive=False)
                want = sinking_per_pay(unfunded, remaining_paydays)
                items.append(
                    (
                        not bill.is_critical,
                        due,
                        bill.id,
                        f"bill:{bill.id}:{due.isoformat()}",
                        bill,
                        want,
                        "sinking_fund",
                        {
                            "bill_id": bill.id,
                            "due_date": due,
                            "unfunded": cents(unfunded),
                            "paydays_remaining": remaining_paydays,
                        },
                    )
                )
                continue
            for due in bill_due_dates(bill, inputs.payday, self.next_payday):
                items.append(
                    (
                        not bill.is_critical,
                        due,
                        bill.id,
                        f"bill:{bill.id}:{due.isoformat()}",
                        bill,
                        bill.amount,
                        "bill_due_in_window",
                        {"bill_id": bill.id, "due_date": due},
                    )
                )
        items.sort(key=lambda item: (item[0], item[1], item[2]))
        with localcontext(ledger_context()):
            needed = sum((item[5] for item in items), ZERO)
        if needed > self.remaining:
            self.status = PlanStatus.SHORTFALL
        for _, due, bill_id, line_id, bill, want, code, values in items:
            account = (
                self._account(bill.pay_from_account_id, purpose=f"bill {bill_id}")
                if bill.pay_from_account_id is not None
                else None
            )
            got = self._give(line_id, Bucket.BILLS, account, want, code, **values)
            if got < want:
                self._notice(
                    "bill_uncovered",
                    bill_id=bill_id,
                    due_date=due,
                    critical=bill.is_critical,
                    needed=want,
                    covered=got,
                    short=want - got,
                )

    def essentials(self) -> None:
        inputs = self.inputs
        count = max(1, paydays_in_month(inputs.schedule, inputs.bank_holidays, inputs.payday.year, inputs.payday.month))
        for line in sorted(inputs.budget_lines, key=lambda b: b.id):
            if line.period is BudgetPeriod.PER_PAY:
                want = line.amount
                values: dict[str, object] = {"budget_line_id": line.id, "basis": "per_pay"}
            else:
                want = floor_cents(divide(line.amount, Decimal(count)))
                values = {"budget_line_id": line.id, "basis": "per_month", "paydays_in_month": count}
            account = self._account(line.account_id, purpose=f"budget line {line.id}") if line.account_id else None
            self._give(f"essential:{line.id}", Bucket.ESSENTIALS, account, want, "budget_line", **values)

    def buffer(self) -> None:
        if self.policy.buffer is None:
            return
        account = self._role_account("buffer")
        if account is None:
            self._notice("bucket_skipped", bucket="buffer", why="no_account_with_role")
            return
        target = self.policy.buffer.target
        need = max(target - account.projected_balance, ZERO)
        self._give(
            f"buffer:{account.id}",
            Bucket.BUFFER,
            account,
            need,
            "buffer_top_up",
            target=target,
            projected_balance=account.projected_balance,
        )

    def emergency_fund(self) -> None:
        ef = self.policy.emergency_fund
        if ef is None:
            return
        account = self._role_account("emergency_fund")
        if account is None:
            self._notice("bucket_skipped", bucket="emergency_fund", why="no_account_with_role")
            return
        monthly = monthly_essentials(self.inputs)
        target = floor_cents(multiply(ef.months, monthly))
        need = max(target - account.projected_balance, ZERO)
        cap = cap_amount(ef.per_pay_cap, self.inputs.net_pay)
        self._give(
            f"emergency_fund:{account.id}",
            Bucket.EMERGENCY_FUND,
            account,
            min(need, cap),
            "emergency_fund",
            target=target,
            months=ef.months,
            monthly_essentials=floor_cents(monthly),
            projected_balance=account.projected_balance,
            per_pay_cap=cap,
        )

    def goals(self) -> None:
        payday = self.inputs.payday
        for goal in sorted(self.inputs.goals, key=lambda g: (g.priority, g.id)):
            if not goal.active:
                continue
            account = self.accounts.get(goal.account_id)
            if account is None:
                raise InputError(f"goal {goal.id}: unknown account {goal.account_id!r}")
            if account.currency != "CAD":
                self._notice("goal_skipped_non_cad", goal_id=goal.id, account_id=account.id)
                continue
            outstanding = max(goal.target - goal.saved, ZERO)
            if outstanding == 0:
                continue
            want, code, values = self._goal_want(goal, outstanding, payday)
            if code == "goal_overdue":
                self._notice("goal_overdue", goal_id=goal.id, target_date=goal.target_date, outstanding=outstanding)
            self._give(f"goal:{goal.id}", Bucket.GOALS, account, want, code, **values)

    def _goal_want(self, goal: Goal, outstanding: Decimal, payday: date) -> tuple[Decimal, str, dict[str, object]]:
        policy = self.policy.goals
        if goal.target_date is None:
            return (
                min(policy.undated_per_pay, outstanding),
                "goal_undated",
                {"goal_id": goal.id, "outstanding": outstanding, "undated_per_pay": policy.undated_per_pay},
            )
        if goal.target_date < payday:
            want = outstanding
            if policy.overdue_per_pay_cap is not None:
                want = min(want, cap_amount(policy.overdue_per_pay_cap, self.inputs.net_pay))
            return (
                want,
                "goal_overdue",
                {"goal_id": goal.id, "target_date": goal.target_date, "outstanding": outstanding},
            )
        left = self._paydays_before(goal.target_date, inclusive=True)
        return (
            sinking_per_pay(outstanding, left),
            "goal_on_track",
            {
                "goal_id": goal.id,
                "target_date": goal.target_date,
                "outstanding": outstanding,
                "paydays_remaining": left,
            },
        )

    def investing(self) -> None:
        inv = self.policy.investing
        if inv is None:
            return
        limit = cap_amount(inv.per_pay_limit, self.inputs.net_pay)
        left = limit
        for account_id in inv.targets:
            account = self._account(account_id, purpose="investing target")
            if account.kind not in REGISTERED and account.kind is not AccountKind.NON_REGISTERED:
                raise PolicyError(f"investing target {account_id!r} is a {account.kind} account")
            room = self.room_left.get(account.kind) if account.registered else None
            got = self._give(
                f"investing:{account.id}",
                Bucket.INVESTING,
                account,
                left,
                "investing",
                account_kind=account.kind.value,
                per_pay_limit=limit,
                room_before=room if room is not None else ("stale" if account.registered else "n/a"),
            )
            left -= got

    def explore(self) -> None:
        ex = self.policy.explore
        if ex is None:
            return
        account = self._account(ex.account_id, purpose="explore account")
        want = percent_of(ex.percent, self.inputs.net_pay)
        self._give(f"explore:{account.id}", Bucket.EXPLORE, account, want, "explore", percent=ex.percent)

    def spending(self) -> None:
        surplus = self.policy.surplus
        if surplus is None:
            self._give(SPENDING_LINE_ID, Bucket.SPENDING, None, self.remaining, "spending_remainder")
            return
        account = self._account(surplus.redirect_to, purpose="surplus redirect")
        if account.registered:
            raise PolicyError("surplus can't be redirected to a registered account")
        cap = cap_amount(surplus.spending_cap, self.inputs.net_pay)
        self._give(SPENDING_LINE_ID, Bucket.SPENDING, None, min(cap, self.remaining), "spending_remainder", cap=cap)
        self._give(f"surplus:{account.id}", Bucket.SURPLUS, account, self.remaining, "surplus_redirect")

    # -- run --------------------------------------------------------------------------------------

    def run(self) -> Plan:
        inputs = self.inputs
        if inputs.expected_pay is not None and inputs.expected_pay != inputs.net_pay:
            self._notice(
                "pay_differs_from_expected",
                expected=inputs.expected_pay,
                actual=inputs.net_pay,
                difference=inputs.net_pay - inputs.expected_pay,
            )
        self.bills()
        self.essentials()
        middle = {
            MiddleBucket.BUFFER: self.buffer,
            MiddleBucket.EMERGENCY_FUND: self.emergency_fund,
            MiddleBucket.GOALS: self.goals,
            MiddleBucket.INVESTING: self.investing,
            MiddleBucket.EXPLORE: self.explore,
        }
        for bucket in self.policy.order:
            middle[bucket]()
        self.spending()
        plan = Plan(
            policy_version=self.policy.version,
            inputs_hash=snapshot_hash(inputs, self.policy),
            payday=inputs.payday,
            next_payday=self.next_payday,
            net_pay=inputs.net_pay,
            status=self.status,
            lines=tuple(self.lines),
            notices=tuple(self.notices),
        )
        if plan.total() != inputs.net_pay:  # invariant 1: fail closed, never return a leaking plan
            raise AssertionError(f"conservation violated: {plan.total()} != {inputs.net_pay}")
        return plan


def allocate(inputs: LedgerInputs, policy: AllocationPolicy) -> Plan:
    """Compute the allocation plan for one paycheque. Pure and deterministic."""
    with localcontext(ledger_context()):
        return _Allocator(inputs, policy).run()


# ---------------------------------------------------------------------------------------------------
# Revisions (spec section 5)


def revise(plan: Plan, inputs: LedgerInputs, policy: AllocationPolicy, overrides: Mapping[str, Decimal]) -> Plan:
    """Apply line overrides and rebalance only the spending line.

    Rejected with a typed error if the plan doesn't match these inputs, a line is unknown, the spending
    line itself is overridden, an amount isn't whole non-negative cents, spending would go negative
    (``ConservationError``), or a registered account type would exceed its room (``RoomExceeded``).
    """
    if plan.inputs_hash != snapshot_hash(inputs, policy):
        raise RevisionError("plan was not computed from these inputs and policy")
    if SPENDING_LINE_ID in overrides:
        raise RevisionError("the spending line is the balancing line and can't be overridden")
    known = {line.line_id for line in plan.lines}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise RevisionError(f"unknown lines: {unknown}")
    new_amounts: dict[str, Decimal] = {}
    for line_id, amount in overrides.items():
        try:
            amount = cents(amount)
        except (TypeError, ValueError) as exc:
            raise RevisionError(f"override for {line_id}: {exc}") from exc
        if amount < 0:
            raise RevisionError(f"override for {line_id} is negative")
        new_amounts[line_id] = amount

    with localcontext(ledger_context()):
        lines: list[Line] = []
        for line in plan.lines:
            if line.line_id in new_amounts and new_amounts[line.line_id] != line.amount:
                lines.append(
                    line.model_copy(
                        update={
                            "amount": new_amounts[line.line_id],
                            "overridden_from": line.overridden_from
                            if line.overridden_from is not None
                            else line.amount,
                        }
                    )
                )
            else:
                lines.append(line)
        others = sum((line.amount for line in lines if line.line_id != SPENDING_LINE_ID), ZERO)
        spending = plan.net_pay - others
        if spending < 0:
            raise ConservationError(f"overrides exceed net pay by {-spending}")
        lines = [
            line.model_copy(update={"amount": spending}) if line.line_id == SPENDING_LINE_ID else line for line in lines
        ]

        accounts = {a.id: a for a in inputs.accounts}
        for kind in sorted(REGISTERED):
            used = sum(
                (
                    line.amount
                    for line in lines
                    if line.account_id is not None
                    and line.account_id in accounts
                    and accounts[line.account_id].kind is kind
                ),
                ZERO,
            )
            status = next((r for r in inputs.room if r.kind is kind and r.year == inputs.payday.year), None)
            allowed = max(status.remaining, ZERO) if status is not None else ZERO
            if used > allowed:
                raise RoomExceeded(f"{kind.value}: {used} planned, {allowed} room")

    notices = list(plan.notices)
    for line_id in sorted(new_amounts):
        before = plan.line(line_id).amount
        if new_amounts[line_id] != before:
            notices.append(
                Notice(
                    code="line_overridden",
                    params=params({"line_id": line_id, "from": before, "to": new_amounts[line_id]}),
                )
            )
    revised = plan.model_copy(update={"lines": tuple(lines), "notices": tuple(notices)})
    if revised.total() != plan.net_pay:  # pragma: no cover - guarded above
        raise ConservationError("revision broke conservation")
    return Plan.model_validate(revised.model_dump())
