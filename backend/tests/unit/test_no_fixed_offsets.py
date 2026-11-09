"""Decision 2026-11-09: production code must never use a fixed UTC offset for Toronto.

All Toronto logic goes through ``app.domain.market_time.TORONTO`` (IANA ``America/Toronto``).
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
SOURCE_OF_TRUTH = APP / "domain" / "market_time.py"

FORBIDDEN = [
    (re.compile(r"timezone\(\s*-?\s*timedelta"), "datetime.timezone(timedelta(...)) fixed offset"),
    (re.compile(r"timedelta\(\s*hours\s*=\s*-\s*[45]\b"), "hard-coded -4/-5 hour offset"),
    (re.compile(r"""["'][+-]0?[45]:?00["']"""), "hard-coded -05:00 / -04:00 offset string"),
    (re.compile(r"""ZoneInfo\(\s*["'](EST|EDT|EST5EDT|US/Eastern|Etc/GMT[+-]\d+)["']"""), "non-IANA-Toronto zone"),
    (re.compile(r"\bUTC\s*[+-]\s*[45]\b"), "UTC-5 style offset"),
]


def _sources() -> list[Path]:
    return sorted(APP.rglob("*.py"))


def test_no_fixed_toronto_offsets_in_production_code():
    hits = []
    for py in _sources():
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            for pattern, why in FORBIDDEN:
                if pattern.search(line):
                    hits.append(f"{py.relative_to(APP)}:{lineno}: {why}: {line.strip()}")
    assert hits == []


def test_toronto_zone_is_constructed_in_exactly_one_place():
    constructors = [py for py in _sources() if re.search(r"ZoneInfo\(", py.read_text(encoding="utf-8"))]
    assert constructors == [SOURCE_OF_TRUTH]
    assert 'ZoneInfo("America/Toronto")' in SOURCE_OF_TRUTH.read_text(encoding="utf-8")


def test_scanner_actually_catches_offenders(tmp_path):
    """Guard the guard: each forbidden pattern matches a representative offender."""
    offenders = [
        "ET = timezone(timedelta(hours=-5))",
        "offset = timedelta(hours=-4)",
        'stamp = "2026-11-01T02:00:00" + "-05:00"',
        'tz = ZoneInfo("US/Eastern")',
        "# market opens 09:30 UTC-5",
    ]
    for line in offenders:
        assert any(p.search(line) for p, _ in FORBIDDEN), line
