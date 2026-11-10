"""Scan files destined for the public repo for real account data. Used by tests/unit/test_fixture_guard.py.

A violation is any of:
- a GUID outside ``app.redaction.FAKE_GUID_SET`` (all fixture IDs must come from the fixed fake set);
- a standalone 7-10 digit run (the shape of an account number) anywhere in the text;
- a JSON value under an account-number key that is not masked as ``****`` plus at most 4 characters;
- an email address other than the redaction placeholder.

It fails closed: a 7+ digit balance or an epoch-seconds timestamp is flagged too. Redact or round it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.redaction import ACCOUNT_NUMBER_KEY_RE, ACCOUNT_NUMBER_RE, EMAIL_RE, FAKE_GUID_SET, GUID_RE, MASKED_RE

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


def _json_key_violations(obj: Any, path: str = "$") -> Iterator[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if (
                ACCOUNT_NUMBER_KEY_RE.search(str(k))
                and isinstance(v, str | int)
                and not (isinstance(v, str) and MASKED_RE.match(v))
            ):
                yield f"{path}.{k}: account-number field not masked (****1234)"
            yield from _json_key_violations(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _json_key_violations(v, f"{path}[{i}]")


def scan_text(text: str, *, is_json: bool = False) -> list[str]:
    problems: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in GUID_RE.finditer(line):
            if m.group(0).lower() not in FAKE_GUID_SET:
                problems.append(f"line {lineno}: GUID {m.group(0)[:8]}... is not in FAKE_GUIDS")
        for m in ACCOUNT_NUMBER_RE.finditer(GUID_RE.sub("", line)):
            problems.append(f"line {lineno}: account-number-shaped value ...{m.group(0)[-4:]}")
        for m in EMAIL_RE.finditer(line):
            if m.group(0) not in ALLOWED_EMAILS:
                problems.append(f"line {lineno}: email address")
    if is_json:
        docs: list[Any] = []
        try:
            docs = [json.loads(text)]
        except ValueError:
            try:  # JSON Lines
                docs = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
            except ValueError:
                problems.append("unparseable JSON: cannot verify account-number fields")
        for d in docs:
            problems.extend(_json_key_violations(d))
    return problems


def scan_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [f"{path.relative_to(REPO)}: {p}" for p in scan_text(text, is_json=path.suffix in {".json", ".jsonl"})]
