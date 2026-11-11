"""Break-test the ledger safety tests: apply one mutation at a time and check the guarding test fails.

**Runs only inside a temporary copy of backend/, never in the repository.** It edits source files, so
an interruption must never be able to leave a mutation in real code (this happened once, 2026-11-10).
At start-up it refuses to run unless the working directory:

- is inside the system temporary directory,
- has no ``.git`` in it or in any parent (so it isn't a checkout or inside one), and
- looks like a backend copy (``app/ledger`` and ``tests`` exist).

Usage (Git Bash; from the repo root):

    T=$(mktemp -d) && cp -r backend/app backend/tests backend/scripts backend/pyproject.toml "$T/"
    cd "$T" && PYTHONPATH="$T" uv run --project <repo>/backend python scripts/break_test.py

Each mutation is restored from a byte copy whether the test run passes, fails or is interrupted.
Exit code 0 means every mutation was caught.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


class UnsafeWorkdir(RuntimeError):
    """The working directory is not a disposable copy."""


def ensure_disposable_copy(workdir: Path, temp_root: Path | None = None) -> Path:
    """Return the resolved ``workdir`` if it is a temporary copy outside any repository, else raise."""
    resolved = workdir.resolve()
    temp = (temp_root or Path(tempfile.gettempdir())).resolve()
    if resolved == temp or temp not in resolved.parents:
        raise UnsafeWorkdir(f"{resolved} is not inside the temporary directory {temp}")
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            raise UnsafeWorkdir(f"{resolved} is inside a git checkout ({candidate})")
    if not (resolved / "app" / "ledger").is_dir() or not (resolved / "tests").is_dir():
        raise UnsafeWorkdir(f"{resolved} does not look like a copy of backend/ (no app/ledger or tests)")
    return resolved


@dataclass(frozen=True)
class Edit:
    path: str
    old: str
    new: str
    #: Replace every occurrence (the anchor must still occur at least once).
    every: bool = False


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    test: str
    #: Further edits applied together with the first (for defences with more than one layer).
    also: tuple[Edit, ...] = ()

    def edits(self) -> tuple[Edit, ...]:
        return (Edit(self.path, self.old, self.new), *self.also)


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "conservation",
        "app/ledger/engine.py",
        'self._give(SPENDING_LINE_ID, Bucket.SPENDING, None, self.remaining, "spending_remainder")',
        'self._give(SPENDING_LINE_ID, Bucket.SPENDING, None, self.remaining - Decimal("0.01")'
        ' if self.remaining else self.remaining, "spending_remainder")',
        "tests/unit/ledger/test_properties.py::test_conservation_and_no_negative_lines",
    ),
    Mutation(
        "room cap",
        "app/ledger/engine.py",
        "            amount = min(amount, room)\n",
        "",
        "tests/unit/ledger/test_properties.py::test_registered_accounts_never_exceed_room",
    ),
    Mutation(
        "stale room",
        "app/ledger/engine.py",
        "            return ZERO\n        return left",
        '            return Decimal("99999.00")\n        return left',
        "tests/unit/ledger/test_golden.py::test_january_with_stale_contribution_room",
    ),
    Mutation(
        "withdrawals add room",
        "app/ledger/room.py",
        "and m.movement is MovementType.CONTRIBUTION",
        "",
        "tests/unit/ledger/test_room.py::test_withdrawals_never_increase_remaining_room",
    ),
    Mutation(
        "critical first",
        "app/ledger/engine.py",
        "items.sort(key=lambda item: (item[0], item[1], item[2]))",
        "items.sort(key=lambda item: (item[1], item[2]))",
        "tests/unit/ledger/test_golden.py::test_shortfall_autopay_due_later_beats_manual_due_earlier",
    ),
    Mutation(
        "sinking guard",
        "app/ledger/engine.py",
        "    if paydays_remaining <= 0:\n        return unfunded\n",
        "",
        "tests/unit/ledger/test_engine_rules.py::test_sinking_per_pay_guard_and_rounding",
    ),
    Mutation(
        "round down",
        "app/ledger/money.py",
        "rounding=ROUND_FLOOR)",
        "rounding=ROUND_HALF_EVEN)",
        "tests/unit/ledger/test_engine_rules.py::test_rounding_is_down",
    ),
    Mutation(
        "shortfall zeroes",
        "app/ledger/engine.py",
        "        amount = min(max(want, ZERO), self.remaining)\n",
        "        amount = min(max(want, ZERO), self.remaining) if bucket is Bucket.BILLS else max(want, ZERO)\n",
        "tests/unit/ledger/test_golden.py::test_shortfall_month",
    ),
    Mutation(
        "spending cap (P19)",
        "app/ledger/engine.py",
        "min(cap, self.remaining)",
        "self.remaining",
        "tests/unit/ledger/test_properties.py::test_spending_never_exceeds_an_active_cap",
    ),
    Mutation(
        "revision spending cap (P19)",
        "app/ledger/engine.py",
        "            spending = min(left, cap_amount(policy.surplus.spending_cap, plan.net_pay))\n",
        "",
        "tests/unit/ledger/test_properties.py::test_spending_never_exceeds_an_active_cap",
    ),
    Mutation(
        "order notice (P18)",
        "app/ledger/engine.py",
        "        self.order_warnings()\n",
        "",
        "tests/unit/ledger/test_golden.py::test_investing_ahead_of_an_underfunded_emergency_fund_gets_a_notice",
    ),
    Mutation(
        "import ban",
        "app/ledger/engine.py",
        "from collections.abc import Mapping\n",
        "from collections.abc import Mapping\nimport time  # noqa\n",
        "tests/unit/ledger/test_invariants.py::test_no_forbidden_imports",
    ),
    Mutation(
        "clock ban",
        "app/ledger/schedule.py",
        "_DAY = timedelta(days=1)\n",
        "_DAY = timedelta(days=1)\n_T = date.today()\n",
        "tests/unit/ledger/test_invariants.py::test_no_clock_randomness_or_io_calls",
    ),
    Mutation(
        "float ban",
        "app/ledger/money.py",
        'CENT = Decimal("0.01")\n',
        'CENT = Decimal("0.01")\n_F = 0.5\n',
        "tests/unit/ledger/test_invariants.py::test_no_floats",
    ),
    Mutation(
        "tax literal",
        "app/ledger/policy.py",
        "class FixedCap(Frozen):",
        "TFSA_LIMIT = 7000\n\n\nclass FixedCap(Frozen):",
        "tests/unit/ledger/test_invariants.py::test_no_tax_constants",
    ),
    Mutation(
        "tax Decimal",
        "app/ledger/policy.py",
        "class FixedCap(Frozen):",
        'INCLUSION = Decimal("0.5")\n\n\nclass FixedCap(Frozen):',
        "tests/unit/ledger/test_invariants.py::test_no_tax_constants",
    ),
    Mutation(
        "snapshot floats",
        "app/ledger/snapshot.py",
        "data = json.loads(raw, parse_float=_refuse_float)\n    assert_no_floats(data)",
        "data = json.loads(raw)",
        "tests/unit/ledger/test_engine_rules.py::test_snapshot_refuses_floats",
    ),
    Mutation(
        "frozen plan",
        "app/ledger/models.py",
        "frozen=True, strict=True",
        "frozen=False, strict=True",
        "tests/unit/ledger/test_engine_rules.py::test_plan_is_frozen",
    ),
    Mutation(
        "strict models + types",  # two layers: the model config and the annotated money types
        "app/ledger/models.py",
        "frozen=True, strict=True",
        "frozen=True, strict=False",
        "tests/unit/ledger/test_engine_rules.py::test_models_reject_floats_and_sub_cent_amounts",
        also=(Edit("app/ledger/money.py", "Strict(), ", "", every=True),),
    ),
    Mutation(
        "revision room",
        "app/ledger/engine.py",
        "            if used > allowed:",
        "            if False:",
        "tests/unit/ledger/test_golden.py::test_revision_with_a_line_override",
    ),
    # -- calculators (M2) ---------------------------------------------------------------------------
    Mutation(
        "calc: rate default",
        "app/ledger/calculators/loan.py",
        "    annual_rate: Percent\n",
        "    annual_rate: Percent = ZERO\n",
        "tests/unit/ledger/calculators/test_invariants.py::test_every_rate_is_required",
    ),
    Mutation(
        "calc: tax literal scanned",
        "app/ledger/calculators/registered.py",
        "    marginal_rate_now: Percent\n",
        '    marginal_rate_now: Percent = Decimal("33")\n',
        "tests/unit/ledger/test_invariants.py::test_no_tax_constants",
    ),
    Mutation(
        "calc: floats accepted",  # two layers, as for the engine models
        "app/ledger/models.py",
        "frozen=True, strict=True",
        "frozen=True, strict=False",
        "tests/unit/ledger/calculators/test_invariants.py::test_rejects_floats_for_every_decimal_input",
        also=(Edit("app/ledger/money.py", "Strict(), ", "", every=True),),
    ),
    Mutation(
        "calc: half-up rounding",
        "app/ledger/money.py",
        "rounding=ROUND_HALF_UP",
        "rounding=ROUND_HALF_EVEN",
        "tests/unit/ledger/calculators/test_reference_cases.py::test_gic_textbook_half_up_rounding",
    ),
    Mutation(
        "calc: loan payment rounds up",
        "app/ledger/calculators/loan.py",
        "payment = ceil_cents(divide(multiply(inputs.principal, i)",
        "payment = half_up_cents(divide(multiply(inputs.principal, i)",
        "tests/unit/ledger/calculators/test_reference_cases.py::test_loan_textbook_canadian_mortgage_semiannual_compounding",
    ),
    Mutation(
        "calc: loan final payment",
        "app/ledger/calculators/loan.py",
        "LoanRow(number=number, payment=balance + interest,",
        "LoanRow(number=number, payment=payment,",
        "tests/unit/ledger/calculators/test_properties.py::test_loan_schedule_is_exact",
    ),
    Mutation(
        "calc: loan first-row guard",
        "app/ledger/calculators/loan.py",
        "    if n > 1 and first_interest >= payment:",
        "    if False:",
        "tests/unit/ledger/calculators/test_reference_cases.py::test_loan_too_small_to_amortize_in_cents_is_refused",
    ),
    Mutation(
        "calc: goal rounds up",
        "app/ledger/calculators/savings.py",
        "per_pay=ceil_cents(",
        "per_pay=half_up_cents(",
        "tests/unit/ledger/calculators/test_reference_cases.py::test_goal_rounds_up_so_the_target_is_reached",
    ),
    Mutation(
        "calc: runway rounds down",
        "app/ledger/calculators/savings.py",
        "rounding=ROUND_FLOOR",
        'rounding="ROUND_HALF_UP"',
        "tests/unit/ledger/calculators/test_reference_cases.py::test_runway",
    ),
    Mutation(
        "break-test guard",
        "scripts/break_test.py",
        # Built with + so the joined text occurs only in the guard itself, not in this table.
        "        raise UnsafeWorkdir(" + 'f"{resolved} is inside a git checkout ({candidate})")',
        "        pass",
        "tests/unit/test_break_test_guard.py",
    ),
)


def run_one(root: Path, mutation: Mutation) -> bool:
    """Apply every edit, test, restore all. True if the guarding test failed (the mutation was caught)."""
    originals: dict[Path, bytes] = {}
    try:
        for edit in mutation.edits():
            path = root / edit.path
            originals.setdefault(path, path.read_bytes())
            text = path.read_text(encoding="utf-8")
            found = text.count(edit.old)
            if found == 0 or (found != 1 and not edit.every):
                raise RuntimeError(f"{mutation.name}: anchor found {found} times in {edit.path}")
            path.write_bytes(text.replace(edit.old, edit.new).encode("utf-8"))
        result = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
            [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", mutation.test],
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root)},
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        for path, original in originals.items():
            path.write_bytes(original)
    return result.returncode != 0


def main() -> int:
    try:
        root = ensure_disposable_copy(Path.cwd())
    except UnsafeWorkdir as exc:
        print(f"break_test: REFUSED. {exc}", file=sys.stderr)
        return 2
    missed = 0
    for mutation in MUTATIONS:
        caught = run_one(root, mutation)
        missed += not caught
        print(f"{'CAUGHT' if caught else 'MISSED'}  {mutation.name:28} -> {mutation.test.split('::')[-1]}")
    print("ALL CAUGHT" if not missed else f"{missed} MISSED")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
