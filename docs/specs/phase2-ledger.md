# Aurelius — Phase 2: The Ledger Core (payday engine, budgets, calculators)

> **Kickoff message to paste into Claude Code:**
> `Save the attached brief as docs/specs/phase2-ledger.md and commit it. Read it in full together with CLAUDE.md, then propose a milestone plan for Phase 2 and stop for my OK before building.`

---

## Decisions (2026-11-10). These win over the brief below.

| # | Decision |
|---|---|
| P1 | **Milestones.** M1 is the pure engine (dates, policy, waterfall, revision, snapshots, reconciliation and room math, with property and golden tests). M2 is the calculators. M3 is persistence and services (Postgres 16, SQLAlchemy 2 async, Alembic, seed). M4 is the API (FastAPI, bearer auth, RFC 9457). |
| P2 | **The brief's assumptions were wrong.** There is no FastAPI service, bearer auth, Postgres or `Money` class yet; Phase 2 builds them (M3, M4). "Money" means a CAD `Decimal` validated to whole cents (`app/ledger/money.py`). |
| P3 | **Dependencies.** hypothesis (dev) and uvicorn (no extras, M4) are approved. The seed file is `seed.toml`, read with stdlib `tomllib`; no YAML library. |
| P4 | **Rounding.** Every computed amount (a percentage cap, a per-month line converted per pay, a goal or sinking amount per pay) is rounded **down** to the cent. The remainder flows to spending. |
| P5 | **Per-month budget lines.** The per-pay amount is the monthly amount divided by the number of scheduled paydays in the payday's calendar month, so a three-paycheque month gives one third per pay. |
| P6 | **Emergency-fund target.** N × the monthly equivalent of **essential** bills (`essential` flag, default true) plus all budget lines. Monthly equivalents: annual ÷ 12, every n months ÷ n, biweekly × 26 ÷ 12, per-pay lines × paydays per year ÷ 12. |
| P7 | **Sinking funds.** Per pay = (amount − already set aside) ÷ paydays remaining before the due date, counting this one. "Already set aside" is the approved sinking lines since the bill was last due. In the window where it falls due, only the unfunded part is allocated. **Guard:** with zero paydays remaining (for example, due on the payday itself), allocate the full unfunded amount; never divide by zero. |
| P8 | **Projected balances and reconciliation.** Projected balance = the latest balance entry plus approved plan lines into that account dated after it (approved transfers are assumed to have been made). When a new balance entry arrives, record the drift (actual − projected): it is reported in `/ledger/summary` and stored as an append-only record. Projection then starts from the actual balance. |
| P9 | **Contribution room.** Remaining room = the user-entered room for the year − contributions dated on or after its as-of date in that year. Contributions are approved plan lines plus optional manual contribution entries for transfers made outside Aurelius. **Withdrawals never increase remaining room within a calendar year, for any account type.** Room changes only through user-entered annual figures. |
| P10 | **Goals.** A goal with no target date gets the policy's fixed undated per-pay amount (possibly zero). A goal past its date is flagged and keeps receiving its remaining amount (capped by the policy, if it sets a cap) until done or cancelled. |
| P11 | **Shortfall order.** Critical bills first (autopay, or the user-set `critical` flag), then the rest. Each group is in due-date order. The first bill that doesn't fit gets a partial line, every uncovered amount is listed, and essentials and buckets 3–8 get 0. |
| P12 | **Long-term investing** has a per-pay limit set by the policy, as a fixed amount or a percentage of net pay. |
| P13 | **Semimonthly pay** uses two configured days of the month (default: the 15th and the last day). |
| P14 | **Currency.** Allocation is CAD only. USD accounts may hold balances but are never allocation targets. |
| P15 | **DST.** Pure date math has no DST. DST tests apply where the service turns "now" into a Toronto date (M3), around 2026-11-01 and 2027-03-14. |
| P16 | **Merges.** Rudra merges every PR himself. Claude Code opens the PR and reports CI. |
| P17 | **Monotonicity under cent rounding.** Every cap *amount* (a fixed cap, or a percentage of net pay rounded down) is non-decreasing in net pay. When net pay increases, a bucket's *allocation* may decrease by at most one cent per percentage cap in the policy. This applies to percentage-capped buckets too, when they are limited by what remains after other percentage caps. Example: with 10% and 20% caps above it, an 80%-capped investing bucket goes 0.15 → 0.14 as net pay goes 0.19 → 0.20. Strict monotonicity across several independently rounded percentage caps is impossible. This is the same kind of obstruction as the Balinski–Young impossibility theorem in apportionment (no method that stays within quota avoids every monotonicity paradox), shown here directly by the counterexamples in `test_properties.py`. The bounded property test is the rule; the strict version is kept as a strict xfail. |
| P18 | **Bucket order.** Bills and essentials always come first and spending last; the policy orders only buffer, emergency fund, goals, investing and explore. If investing or explore is ordered ahead of an emergency fund that is below target, the plan carries a non-blocking `ordered_ahead_of_underfunded_emergency_fund` notice. |
| P19 | **Surplus redirect.** Without a spending cap, spending receives everything left. With a cap, spending never exceeds it, and every leftover cent (rounding remainder included) goes to the redirect account, which can't be a registered account. Revisions keep this: with a cap active, spending and the surplus line are both balancing lines, and neither can be overridden. |
| P20 | **Compound growth (M2).** Nominal periodic rate `i = r / n`, contribution at the **end** of each period (ordinary annuity): `B_k = B_{k-1}(1 + i) + C`. Rates are entered in percent, and a negative return (above -100%) is allowed. |
| P21 | **Calculator rounding.** Calculators compute at full Decimal precision and round to the cent only at output. Projections (growth, fee drag, GIC values, TFSA vs RRSP, FI number) round **half-up**. Derived figures are taken from rounded values, so they add up exactly (e.g. `cost_of_fees = without - with`). |
| P22 | **Loan amortization, lender-style.** `i = (1 + r/(100m))^(m/p) - 1` (m = 2 for a Canadian fixed mortgage). The payment is the annuity payment rounded **up** to the cent. Interest per row is rounded half-up, and the final payment absorbs the remainder, so the principal column sums exactly to the loan. Published examples that round the payment to the nearest cent can differ by one cent (e.g. 581.61 vs 581.60). A loan too small to amortize in whole cents (the first row's rounded interest ≥ payment) raises `CalculatorError`. |
| P23 | **Goal planner.** Per pay = outstanding ÷ adjusted paydays in `[as_of, target_date]` (a payday on the target date counts, as in the engine), rounded **up** to the cent, so following it reaches the target. With no payday left the status is `no_paydays_remaining` and there is no per-pay figure. The engine still rounds down (P4) and catches up each pay. |
| P24 | **Fee drag.** The fee is subtracted from the return: growth at `r - MER` versus growth at `r`. |
| P25 | **TFSA vs RRSP.** One lump contribution of `C` pre-tax dollars, compounded annually for `Y` years: `TFSA = C·g·(1 - t_now)`, `RRSP = C·g·(1 - t_later)`, with `g = (1 + r)^Y`. They are equal when the two rates are equal. |
| P26 | **GIC ladder.** The amount is split equally across rungs, with the leftover cents on rung 1. Rung k is a `k × term` (whole years) GIC bought on the start date at the rate entered for that term, compounding annually. It matures on the same day and month (Feb 29 falls on Feb 28). |
| P27 | **Other calculator rules.** Runway months are rounded **down** to 0.01, so coverage is never overstated. FI number = expenses ÷ withdrawal rate. A zero divisor (essentials, withdrawal rate) is rejected at validation. **Loans: amortization vs term.** `amortization_years` is the time to repay in full. The optional `term_years` (the contract length, at most the amortization) adds a term summary to the same schedule: payments, principal and interest paid within the term, and the balance left at its end (the renewal balance), which is zero when the term equals the amortization. Every rate is a required input with no default (`tests/unit/ledger/calculators/test_invariants.py`). The calculators are pure (`app/ledger/calculators`); their `POST /calculators/<name>` endpoints are built in M4. |

> **P28–P35 are Rudra's M3 kickoff decisions (1a–10a) and the answers of 2026-11-16 to the review questions.** An earlier reconstruction from the code (commit 4763acf) is superseded by this table. P36–P39 below answer the questions that review left open. **Process:** every new decision is written here in the same turn it is received, before building.

| # | Decision |
|---|---|
| P28 | **Persistence (1a, 2a, 3a).** Postgres 16 (P1) via SQLAlchemy 2 async with **asyncpg**; Alembic migrations. **Tests use real Postgres:** CI runs a services container pinned by digest (the same image as `app/db/backup.PG_IMAGE`) with `AURELIUS_REQUIRE_DATABASE=1`, so database tests **fail** in CI if they can't run; locally they use `AURELIUS_TEST_DATABASE_URL` or a Docker fixture, and **skip** when neither is available. **Deployment:** `deploy/compose.yml` binds Postgres to 127.0.0.1, uses a named volume and takes the password from `.env` with no default. `aurelius db backup` writes a timestamped `pg_dump` custom-format file to the gitignored `backend/.local/backups/`; `aurelius db restore <file>` refuses a non-empty database unless `--force`. A test runs backup → restore → every stored snapshot hash verifies. *Not built yet:* `deploy/compose.yml` and the backup → restore → hash test (it needs stored plans). |
| P29 | **Keys and config (4a, 5a).** Config (income sources, accounts, bills, budget lines, goals, bank holidays) has user-chosen slug primary keys, is edited in place, and has an `active` flag; a row that is referenced can't be hard-deleted (no row references a bank holiday, so holidays can be removed). Paycheques, plans, events and every history entry have generated IDs and are append-only (a database trigger rejects updates and deletes). |
| P30 | **Ordering and corrections (7a).** No new paycheque while an earlier plan is unapproved; no backdating; only a proposed plan can be revised (the old version becomes `revised`, the new one links to it by `parent_plan_id`). **Correction path:** only the **most recent** approved plan can be voided, with or without a replacement. Voided lines stop counting toward room, balances, goals and sinking funds at once. A replacement (linked by `replaces_plan_id`) is computed **as of the original payday**: only entries dated on or before it count, and later entries affect the next plan. A void without a replacement also voids the paycheque. Everything is append-only, and every transition appends a `plan_event`. |
| P31 | **Goal progress (8a).** Saved = approved, non-voided goal lines + `saved_before_aurelius` + signed manual adjustments (amount, date, required note). |
| P32 | **Income and paycheques (6a, review answer).** Exactly one active income source. A paycheque's date need not equal the scheduled payday: it belongs to the pay window containing it, with at most one regular paycheque per window. Off-cycle pay (a bonus) is refused with a clear error for now. A paycheque dated earlier than the latest recorded one is refused. |
| P33 | **Reports (10a, review answer).** `summary(month)` and `upcoming` (bills and paydays in the next 30 days from today in Toronto) count **approved, non-voided** plans only; proposed plans are listed separately as pending. **Savings rate** = (emergency fund + goals + long-term investing + explore + surplus redirected to a non-chequing account) ÷ approved net pay for the month. Bills, essentials, buffer, sinking funds and spending are excluded. |
| P34 | **Seed (9a, review answer).** Upsert config by slug; add history entries only if missing; never touch plans or paycheques. Dry run is the default; `--apply` writes. If any row was changed after its last seed (`updated_at > seeded_at`), the **whole** `--apply` run is refused, listing every conflict; `--force` overwrites. One transaction either way. Real data lives in the gitignored `backend/.local/seed.toml`; only `seed.example.toml` with fictional values is committed. |
| P35 | **Scope and time (10a).** M3 delivers the full service layer, including the summary and upcoming queries. Services take an injected clock (an aware UTC instant, `clock.require_utc`) and turn it into a Toronto date; DST tests around 2026-11-01 and 2027-03-14 (P15). |

**Answers of 2026-11-16 to the open questions:**

| # | Decision |
|---|---|
| P36 | **Projected balance counts kept money only** (refines P8). Approved lines in the buffer, emergency fund, goals, investing, explore and surplus buckets, and sinking-fund lines, add to the destination account's projection. Bill and budget lines pass through and are spent, so they don't count (`app/ledger/history.account_flows`). Drift is measured against this projection. |
| P37 | **A replacement may correct the net pay.** The replacement plan carries the corrected amount; the paycheque row keeps the amount as first recorded (append-only). |
| P38 | **A replacement uses today's config and the voided plan's policy version.** Config has no history, so "as of the original payday" (P30) filters dated entries (balances, room, room movements, goal adjustments) to on or before that payday; bills, goals and accounts are read as they are now. |
| P39 | **No discarding a proposed plan.** A proposed plan can only be revised or approved; the `proposed → voided` transition is removed. A void without a replacement voids the paycheque (a new paycheque state, which needs a migration), and that frees the pay window for a new paycheque. |
| P40 | **Balance entries and drift.** One entry per (account, as-of date). An entry dated earlier than the account's latest entry is refused (`Conflict`). An account's first entry is the baseline and gets no drift record. Every later entry is reconciled against the projection from the previous entry (P36) through its own date, and one drift row is stored for it, **including a zero difference**. |

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
