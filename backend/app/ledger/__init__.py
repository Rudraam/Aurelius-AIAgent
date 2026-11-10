"""Phase 2 ledger core: the pure payday allocation engine and its supporting math.

Everything in this package is pure (CLAUDE.md, Phase 2 invariant 2): no I/O, no clock, no randomness,
no database, no HTTP, no LLM. ``tests/unit/ledger/test_invariants.py`` enforces the import and
literal rules.
"""
