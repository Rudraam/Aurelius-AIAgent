"""Ledger services (Phase 2 M3): gather engine inputs from Postgres, run the pure engine, store results.

Every service takes an ``AsyncSession`` and works inside the caller's transaction; the caller commits.
Every write takes an explicit ``now`` (an aware UTC datetime), so services never read the clock;
``clock.toronto_today`` turns "now" into a Toronto date where a date is needed (P15).

- ``config``: income sources, accounts, bills, budget lines, goals and bank holidays (editable, P29).
- ``records``: contribution room, room movements, goal adjustments, policy versions (append-only).
- ``balances``: balance entries, projected balances and drift (P8, P36, P40).
- ``history``: approved plan lines loaded for the pure rules in ``app.ledger.history``.
- ``plans``: record a paycheque, revise, approve, void and replace, verify (P30, P32).
- ``reports``: the month summary and the upcoming 30 days (P33).
- ``seed``: the TOML seed, dry run by default (P34).
"""
