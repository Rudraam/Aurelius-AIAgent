"""Supply-chain policy: no dependency or action version younger than 14 days.

Enforced twice for Python: Dependabot's cooldown, and uv itself (``exclude-newer = "14 days"``),
which the lockfile records as a relative span, so ``uv sync --locked`` stays stable over time.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BACKEND = REPO / "backend"
DEPENDABOT = (REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")


def ecosystem_block(name: str) -> str:
    m = re.search(rf"^  - package-ecosystem: {name}\n(.*?)(?=^  - package-ecosystem:|\Z)", DEPENDABOT, re.M | re.S)
    assert m, f"no Dependabot entry for {name}"
    return m.group(1)


def test_dependabot_covers_actions_and_backend_uv():
    assert "    directory: /\n" in ecosystem_block("github-actions")
    assert "    directory: /backend\n" in ecosystem_block("uv")


def test_dependabot_cooldown_weekly_and_grouped_minor_patch():
    for eco in ("github-actions", "uv"):
        b = ecosystem_block(eco)
        assert re.search(r"cooldown:\n      default-days: 14\n", b), eco
        assert "interval: weekly" in b, eco
        assert re.search(r"applies-to: version-updates\n\s+update-types: \[minor, patch\]", b), eco


def test_uv_refuses_versions_younger_than_14_days():
    cfg = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    assert cfg["tool"]["uv"]["exclude-newer"] == "14 days"
    lock = (BACKEND / "uv.lock").read_text(encoding="utf-8")
    assert 'exclude-newer-span = "P14D"' in lock, "uv.lock must be resolved under the 14-day span (run `uv lock`)"
