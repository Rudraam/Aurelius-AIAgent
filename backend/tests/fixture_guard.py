"""Scan files destined for the public repo for real account data. Used by tests/unit/test_fixture_guard.py.

A violation is any of:
- a GUID outside ``app.redaction.FAKE_GUID_SET`` (all fixture IDs must come from the fixed fake set);
- a standalone 7-10 digit run (the shape of an account number);
- a JSON value under an account-number key that is not masked as ``****`` plus at most 4 characters;
- a JSON value under an account-id key that is neither a fake GUID nor the redaction placeholder;
- an email address other than the redaction placeholder.

JSON files are checked field by field: a *number* under an allowlisted market-data field
(``app.redaction.is_market_data_field``: volumes, sizes, prices, returns) may have 7-10 digits. Every other
field fails closed: a 7+ digit balance or an epoch-seconds timestamp is flagged. Redact, round or classify it.
Non-JSON files (Markdown, Python, ...) are scanned line by line with no exemptions.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.redaction import (
    ACCOUNT_ID_KEY_RE,
    ACCOUNT_ID_PLACEHOLDER,
    ACCOUNT_NUMBER_KEY_RE,
    ACCOUNT_NUMBER_RE,
    EMAIL_RE,
    FAKE_GUID_SET,
    GUID_RE,
    MASKED_RE,
    is_market_data_field,
)

REPO = Path(__file__).resolve().parents[2]
#: Every place fixture-like data lives or will live (missing directories are skipped).
FIXTURE_ROOTS = (
    REPO / "backend" / "tests" / "fixtures",
    REPO / "backend" / "tests" / "fake_questrade",
    REPO / "docs" / "spikes",
)
TEXT_SUFFIXES = {".json", ".jsonl", ".yaml", ".yml", ".csv", ".txt", ".md", ".py"}
ALLOWED_EMAILS = {"REDACTED@example.invalid"}


def fixture_files() -> list[Path]:
    return sorted(
        p
        for root in FIXTURE_ROOTS
        if root.exists()
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES and "__pycache__" not in p.parts
    )


def _string_violations(s: str, where: str) -> Iterator[str]:
    for m in GUID_RE.finditer(s):
        if m.group(0).lower() not in FAKE_GUID_SET:
            yield f"{where}: GUID {m.group(0)[:8]}... is not in FAKE_GUIDS"
    for m in ACCOUNT_NUMBER_RE.finditer(GUID_RE.sub("", s)):
        yield f"{where}: account-number-shaped value ...{m.group(0)[-4:]}"
    for m in EMAIL_RE.finditer(s):
        if m.group(0) not in ALLOWED_EMAILS:
            yield f"{where}: email address"


def _json_violations(obj: Any, key: str = "", parent: str = "", path: str = "$") -> Iterator[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            k = str(k)
            where = f"{path}.{k}"
            yield from _string_violations(k, f"{where} (key)")
            if ACCOUNT_NUMBER_KEY_RE.search(k) and isinstance(v, str | int) and not isinstance(v, bool):
                if not (isinstance(v, str) and MASKED_RE.match(v)):
                    yield f"{where}: account-number field not masked (****1234)"
            if ACCOUNT_ID_KEY_RE.match(k) and isinstance(v, str | int) and not isinstance(v, bool):
                if not (isinstance(v, str) and (v.lower() in FAKE_GUID_SET or v == ACCOUNT_ID_PLACEHOLDER)):
                    yield f"{where}: account id is neither a fake GUID nor {ACCOUNT_ID_PLACEHOLDER}"
            yield from _json_violations(v, k, key, where)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _json_violations(v, key, parent, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield from _string_violations(obj, path)
    elif isinstance(obj, int | float) and not isinstance(obj, bool):
        if not is_market_data_field(key, parent):
            yield from _string_violations(json.dumps(obj), path)


def _line_violations(text: str) -> Iterator[str]:
    for lineno, line in enumerate(text.splitlines(), 1):
        yield from _string_violations(line, f"line {lineno}")


def scan_text(text: str, *, is_json: bool = False) -> list[str]:
    if not is_json:
        return list(_line_violations(text))
    try:
        docs = [json.loads(text)]
    except ValueError:
        try:  # JSON Lines
            docs = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        except ValueError:
            return [*_line_violations(text), "unparseable JSON: cannot verify fields"]
    return [p for d in docs for p in _json_violations(d)]


def scan_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [f"{path.relative_to(REPO)}: {p}" for p in scan_text(text, is_json=path.suffix in {".json", ".jsonl"})]
