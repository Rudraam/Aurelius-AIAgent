"""Structural invariants of the ledger package (Phase 2 invariants 2, 3, 6, 8).

- The engine imports nothing from the database, HTTP, broker or LLM layers, and never reads the
  clock or randomness.
- No float literal and no ``float(`` call anywhere in ``app/ledger``.
- No tax-shaped numeric literal: every number in ``app/ledger`` must be on a short allowlist of
  calendar and arithmetic constants. A TFSA limit, a tax rate or an inclusion rate can't sneak in.
"""

from __future__ import annotations

import ast
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[3] / "app" / "ledger"

FORBIDDEN_IMPORT_PREFIXES = (
    "sqlalchemy",
    "asyncpg",
    "alembic",
    "psycopg",
    "fastapi",
    "starlette",
    "uvicorn",
    "httpx",
    "httpx2",
    "requests",
    "urllib",
    "socket",
    "mcp",
    "anthropic",
    "openai",
    "random",
    "secrets",
    "uuid",
    "time",
    "os",
    "subprocess",
    "app.integrations.questrade",
    "app.services",
    "app.jobs",
    "app.cli",
    "app.crypto",
    "app.db",
    "app.api",
)
FORBIDDEN_CALLS = {"now", "today", "utcnow", "time", "perf_counter", "monotonic", "random", "urandom", "open"}

#: Calendar and arithmetic constants only. Anything else is a candidate tax constant: add it here
#: only after checking it is not a tax figure (and never for limits, rates or percentages).
ALLOWED_NUMBERS = {0, 1, 2, 5, 7, 12, 14, 15, 21, 24, 26, 28, 31, 34, 52, 62, 100}
ALLOWED_DECIMAL_STRINGS = {"0.00", "0.01"}


def modules() -> list[Path]:
    found = sorted(LEDGER.rglob("*.py"))
    assert found, "ledger package not found"
    return found


def test_no_forbidden_imports() -> None:
    bad = []
    for path in modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name == p or name.startswith(p + ".") for p in FORBIDDEN_IMPORT_PREFIXES):
                    bad.append(f"{path.name}: import {name}")
    assert not bad, bad


def test_no_clock_randomness_or_io_calls() -> None:
    bad = []
    for path in modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
                if name in FORBIDDEN_CALLS:
                    bad.append(f"{path.name}:{node.lineno} {name}()")
    assert not bad, bad


def test_no_floats() -> None:
    bad = []
    for path in modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                bad.append(f"{path.name}:{node.lineno} float literal {node.value!r}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
                bad.append(f"{path.name}:{node.lineno} float()")
    assert not bad, bad


def test_no_tax_constants() -> None:
    bad = []
    for path in modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
                if abs(node.value) not in ALLOWED_NUMBERS:
                    bad.append(f"{path.name}:{node.lineno} {node.value}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Decimal"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value not in ALLOWED_DECIMAL_STRINGS
            ):
                bad.append(f"{path.name}:{node.lineno} Decimal({node.args[0].value!r})")
    assert not bad, bad
