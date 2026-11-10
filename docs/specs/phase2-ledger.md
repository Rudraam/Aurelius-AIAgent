# Aurelius — Phase 2: The Ledger Core (payday engine, budgets, calculators)

> **Kickoff message to paste into Claude Code:**
> `Save the attached brief as docs/specs/phase2-ledger.md and commit it. Read it in full together with CLAUDE.md, then propose a milestone plan for Phase 2 and stop for my OK before building.`

---

## 0. Where this fits

| Phase | Scope | Status |
|---|---|---|
| 1 | Questrade foundation: auth (CIMD, dormant), guardrails, order state machine, exchange calendars, supply-chain hardening | **M1 done.** M2–M4 (REST read sync, order plans) are blocked until Rudra has an active Questrade account |
| **2** | **Ledger core: pay schedule, bills, goals, manual account balances, the payday allocation engine, contribution room, calculators** | **This brief** |
| 3 | Deploy to the Oracle VM + Expo Android app shell (design system, biometric lock, screens over the Phase 2 API) | Next |
| 4 | CFO agent (Claude API): explanations, "Can I afford it?", monthly report, all over Phase 2 data, with no order capability | Later |
| 5 | Broker integration resumes (Phase 1 M2–M4) once an account is active | Gated |

Phase 2 needs **no broker, no bank connection and no LLM**. It is the part of Aurelius that decides where each paycheque goes. Everything later builds on it.

## 1. Role and mission

You are building the deterministic financial core of Aurelius: the engine that takes a paycheque and produces an exact, explainable allocation plan. This is money arithmetic, so correctness beats features. Every rule must be testable, every plan reproducible, and every cent accounted for.

**Mission:** record income, bills, goals and account balances; on each payday, compute an allocation plan through a configurable priority waterfall; let Rudra review, revise and approve it; keep an immutable history; and expose a set of financial calculators. All through the existing FastAPI service and Postgres.

## 2. Non-negotiable invariants

All existing rules in CLAUDE.md still apply. Add these to CLAUDE.md:

1. **Conservation.** For every plan, the allocation lines sum exactly to the net pay, to the cent. No rounding leaks: the remainder after rounding goes to one designated bucket (spending by default), never lost or created.
2. **The engine is pure.** `allocate(inputs, policy) -> plan` has no I/O, no clock reads, no randomness and no database access. The same inputs and policy version always produce byte-identical output. The service layer gathers inputs and stores results.
3. **No LLM in the engine.** Every line carries a structured reason code plus parameters (for example `bill_due_in_window{bill_id, due_date}`). Phase 4's agent turns these into prose; it never computes numbers.
4. **Approving moves no money.** An approved plan is a record of intent. Aurelius calls no broker and no bank in Phase 2. Transfers are done by Rudra (later: Questrade Flows or order plans in Phase 5).
5. **Approved plans are immutable.** Revisions create a new plan version linked to the previous one. Events are append-only.
6. **No tax law in code.** Contribution limits, tax rates, inclusion rates and similar values are user-entered data with an "as of" date and a source note, never constants. Calculators take rates as inputs and have no defaults for them.
7. **Contribution room is a hard ceiling.** The engine never allocates more to a TFSA, FHSA or RRSP than its recorded remaining room. If room data is missing or stale (no entry for the current calendar year), registered-account allocation is zero and the plan says why.
8. **Money is the existing Money / Decimal type** end to end, with `json_decimal` at input boundaries. No floats anywhere in Phase 2 code paths.
9. **Public repo rule.** Real income, bills, balances and goals never enter git. Tests use fictional numbers. Rudra's real configuration lives only in the database and in a gitignored `backend/.local/` seed file.

## 3. Domain model

### Income and pay schedule
- `income_source`: name, schedule type (`biweekly`, `semimonthly`, `monthly`, `weekly`), anchor date, expected net amount, active flag.
- **Payday adjustment:** if a scheduled payday falls on a weekend, it moves to the previous business day. Also support a manual list of bank-holiday dates (user-entered, not generated from exchange calendars, since bank and exchange holidays differ).
- `paycheque`: income source, actual date received, actual net amount (entered by Rudra), status.
- Next-payday computation is a pure function with tests across month ends, leap years and both DST changes.

### Bills
- `bill`: name, category, amount, frequency (`monthly` on a day of month, `biweekly`, `annual` on a date, `every_n_months`), next due date, autopay flag, the account it's paid from, active flag.
- A bill is **due in a pay window** if its due date falls in `[this payday, next payday)`. Annual and every-n-months bills can be **sinking funds**: a fixed amount set aside every pay, so the bill is fully funded by its due date (configurable per bill).
- **Day-of-month edge cases:** a bill due on the 31st falls on the last day in shorter months. Test February in leap and non-leap years.

### Budget lines
- `budget_line`: essentials that aren't bills (groceries, transport), a fixed amount per pay or per month (converted per pay using the actual number of paydays in that month).

### Accounts (manual balances in Phase 2)
- `account`: kind (`chequing`, `hisa`, `gic`, `tfsa`, `fhsa`, `rrsp`, `non_registered`), institution label, currency, role tags (for example `emergency_fund`, `buffer`).
- `balance_entry`: account, amount, as-of date, source (`manual` now; `questrade_sync` in Phase 5).

### Goals
- `goal`: name, target amount, target date (optional), destination account, priority, status.
- The required contribution per pay is the remaining amount divided by the number of paydays left until the target date. A goal past its date with money still missing gets flagged, not silently dropped.

### Contribution room
- `contribution_room`: account type (TFSA, FHSA, RRSP), calendar year, room amount as entered from CRA My Account, as-of date, source note.
- **Remaining room** = entered room minus contributions recorded since the as-of date, counting approved plan lines toward that account type.
- When a new calendar year starts with no entry for it, the engine treats the room as stale (see invariant 7) and the plan prompts Rudra to enter it.

### Allocation policy (versioned)
- `allocation_policy`: a versioned JSON document validated by a Pydantic schema. It defines the order of buckets, caps, targets and percentages. Editing the policy creates a new version; every plan records the version it used.

## 4. The payday waterfall (default policy)

Buckets are filled in priority order. Each bucket takes what it needs up to its limit, and the rest flows down. All values come from the policy and the data, never hard-coded.

1. **Bills due in this pay window**, plus sinking-fund amounts for annual bills.
2. **Essentials** (budget lines for this pay).
3. **Buffer:** top up the chequing buffer to its target.
4. **Emergency fund:** target = N months of essentials (bills plus budget lines, computed from the data). Contribute up to a per-pay cap (a fixed amount or a percentage of net pay) until the target is reached.
5. **Goals** in priority order, each up to its required per-pay amount.
6. **Long-term investing:** split across registered accounts in the policy's priority order (for example TFSA first), each capped by remaining contribution room, then non-registered.
7. **Explore bucket:** capped percentage of net pay (Phase 1's satellite bucket). Optional.
8. **Spending:** everything left, plus any rounding remainder.

**Shortfall handling:** if net pay can't cover bucket 1, the plan's status is `shortfall`. It covers bills in due-date order, lists exactly which bills are uncovered and by how much, and allocates nothing to buckets 3–8. It never produces a negative line.

**Surplus handling:** amounts beyond every cap fall to spending by default. The policy can redirect them (for example to non-registered investing).

## 5. Plan lifecycle

```
computed ──► proposed ──► approved (immutable)
                │
                └──► revised (Rudra edits lines) ──► new version, proposed again
```

- Rudra can override any line's amount in a revision. The engine then rebalances only the spending line, so conservation still holds. If an override would break conservation or exceed contribution room, it's rejected with a typed error.
- Every transition is an append-only `plan_event`.
- When a paycheque's actual amount differs from the expected amount, the plan is computed on the actual amount, and the difference is shown.

## 6. Calculators

Pure functions with Decimal inputs and outputs, a documented formula in each docstring, and an API endpoint each (`POST /calculators/<name>`). Every rate is an input.

| Calculator | Inputs | Output |
|---|---|---|
| Compound growth with regular contributions | starting amount, contribution per period, periods per year, years, annual return | year-by-year balance, total contributed, total growth |
| Fee drag | same plus annual fee (MER) | ending balance with vs without fee, total cost of fees |
| Emergency-fund runway | fund balance, monthly essentials | months covered |
| Goal planner | target, current, target date, pay schedule | required amount per pay, paydays remaining |
| GIC ladder | amount, number of rungs, term per rung, rates per term | rung schedule with maturity dates and values |
| TFSA vs RRSP comparison | contribution, years, return, marginal tax rate now, expected marginal rate at withdrawal | after-tax value of each |
| Loan / amortization | principal, annual rate, compounding, payments per year, term | payment, schedule, total interest |
| Financial-independence number | annual expenses, withdrawal rate | target portfolio size |

Validate each calculator against hand-checked reference cases in the tests: zero rate, zero contributions, a one-period edge case, and at least one textbook example with a known answer. Rounding rules are explicit: compute internally at full Decimal precision and round to cents only at output.

## 7. API surface (Phase 2)

Single-user bearer auth, as already built. RFC 9457 problem+json errors.

- CRUD: `/income-sources`, `/bills`, `/budget-lines`, `/accounts`, `/balances`, `/goals`, `/contribution-room`, `/allocation-policy` (versioned)
- `POST /paycheques`: record a paycheque, which computes and stores a proposed plan
- `GET /plans/{id}`, `GET /plans?from=&to=`
- `POST /plans/{id}/revise`: line overrides, returns the new version
- `POST /plans/{id}/approve`
- `GET /ledger/summary?month=`: income, bills paid, savings rate, allocation by bucket, goal progress, emergency-fund coverage
- `GET /ledger/upcoming`: bills and paydays in the next 30 days
- `POST /calculators/<name>`

## 8. Data and migrations

- Alembic migrations with working downgrades.
- Money columns `NUMERIC(18,2)` for plan amounts (cents); keep `NUMERIC(18,4)` where Phase 1 needs share quantities.
- `allocation_plan` stores the policy version, the input snapshot hash and a full input snapshot (JSON), so any plan can be recomputed and checked bit for bit.
- A gitignored seed mechanism: `uv run aurelius seed --file backend/.local/seed.yaml` loads Rudra's real income, bills, goals, accounts and room into the database. Commit only `seed.example.yaml` with fictional values.

## 9. Testing requirements

- **Property tests (hypothesis), on the engine:**
  - conservation holds for any inputs;
  - no line is ever negative;
  - more pay never reduces any higher-priority bucket (priority monotonicity);
  - no bucket exceeds its cap or target;
  - no registered account exceeds remaining room;
  - recomputing a stored plan from its snapshot reproduces it exactly.
- **Golden scenarios** (fictional data), as snapshot tests:
  - an ordinary month;
  - a shortfall month;
  - a biweekly schedule's three-paycheque month;
  - an annual bill funded as a sinking fund;
  - January with stale contribution room;
  - a goal past its deadline;
  - a revision with a line override;
  - an actual pay that differs from expected.
- **Date edge cases:** month ends, the 31st in short months, leap years, weekend paydays, user-entered bank holidays, both DST changes.
- **Calculators:** reference cases as described in section 6.
- **Invariant tests:**
  - the engine module imports nothing from the database, HTTP or LLM layers;
  - no float reaches the engine;
  - an approved plan can't be modified;
  - no tax constants appear in code (scan for suspicious literals in calculator and engine modules).
- **Break-test** each safety test, as in Phase 1.

## 10. Definition of done

- [ ] The engine, data model, API and calculators are implemented as specified.
- [ ] All property, golden, date-edge, calculator and invariant tests pass; ruff, mypy --strict and pytest are green in CI.
- [ ] `seed.example.yaml` exists with fictional data, and the real seed path is gitignored.
- [ ] A worked example in `docs/specs/phase2-ledger.md` (appended as "Worked example") walks one fictional paycheque through every bucket with the exact numbers the engine produces.
- [ ] CLAUDE.md has the new invariants.
- [ ] Merged to main through a PR with a merge commit.

## 11. Out of scope

- The Expo app and any UI (Phase 3).
- LLM features (Phase 4).
- Broker or bank connections, real money movement (Phase 5).
- Notifications and scheduling of payday jobs (Phase 3, once deployed).
- Tax-return calculations, investment selection, or any recommendation of specific securities.

## 12. Working agreement

1. Propose a milestone plan (about 4 milestones) and wait for an OK.
2. Build the engine first, as a pure module with its property tests, before any database or API work.
3. Commit at every green checkpoint; work on a feature branch; merge through a PR with a merge commit after CI passes.
4. When a rule here is ambiguous, stop and ask rather than guess. Money rules are product decisions.
