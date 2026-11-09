"""Decision 3: Aurelius code must not use private (underscore) MCP SDK modules or attributes."""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
PRIVATE_IMPORT = re.compile(r"^\s*(from|import)\s+mcp(\.\w+)*\._\w+", re.M)
PRIVATE_FROM = re.compile(r"^\s*from\s+mcp[\w.]*\s+import\s+.*\b_\w+", re.M)


def test_app_uses_only_public_mcp_api():
    offenders = []
    for py in APP.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        if PRIVATE_IMPORT.search(src) or PRIVATE_FROM.search(src):
            offenders.append(str(py.relative_to(APP)))
    assert offenders == []
