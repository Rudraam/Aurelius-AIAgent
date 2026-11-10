"""Claude Code must never be able to call a mutating Questrade MCP tool.

Invariant 8 (developer safety): Claude Code never calls preview_*/create_* or any
non-read tool against live Questrade. With the user's own Claude Code session
logged in to the Questrade MCP server, the enforcement point is the
``permissions.deny`` list in ``.claude/settings.json``. A denied MCP tool is
removed from Claude Code's tool pool entirely (verified live on 2026-11-10,
see docs/spikes/questrade-tools.md).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SETTINGS = REPO / ".claude" / "settings.json"
PREFIX = "mcp__questrade__"

# Every tool that creates, edits, previews or changes anything. Previews are
# included: invariant 8 forbids them too. preview_order_instruction is not
# listed by the server today (33 of 34 documented tools) but is denied in case
# it appears.
MUTATING = frozenset(
    {
        "create_order_instruction",
        "create_option_instruction",
        "preview_order_instruction",
        "preview_option_instruction",
        "create_watchlist",
        "edit_watchlist",
        "preview_custom_index",
        "create_custom_index",
        "edit_custom_index",
        "preview_flow",
        "create_flow",
        "update_flow_status",
    }
)

# Tools observed on 2026-11-10 that are strictly read-only.
READ_ONLY = frozenset(
    {
        "list_accounts",
        "get_balances",
        "get_positions",
        "get_portfolio",
        "get_order_history",
        "get_account_activities",
        "get_quotes",
        "search_symbols",
        "get_option_expiries",
        "get_option_chain",
        "get_historical_data",
        "list_watchlists",
        "get_watchlist",
        "get_instruction",
        "list_instructions",
        "list_custom_indexes",
        "get_custom_index",
        "get_balance_sheet",
        "get_balance_sheet_account",
        "list_flows",
        "get_flow",
        "get_flow_history",
    }
)

READ_VERB = re.compile(r"^(get|list|search)_")


def _permissions() -> dict[str, list[str]]:
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    perms: dict[str, list[str]] = data["permissions"]
    return perms


def test_every_mutating_questrade_tool_is_denied() -> None:
    deny = set(_permissions()["deny"])
    missing = sorted(t for t in MUTATING if PREFIX + t not in deny)
    assert not missing, f"mutating Questrade tools not denied: {missing}"


def test_no_questrade_tool_is_allowed_or_askable() -> None:
    perms = _permissions()
    for key in ("allow", "ask"):
        hits = [r for r in perms.get(key, []) if r.startswith("mcp__questrade")]
        assert not hits, f"Questrade rules in permissions.{key}: {hits}"


def test_questrade_deny_rules_name_only_mutating_tools() -> None:
    # A typo'd deny rule silently denies nothing; an unexpected one hides a read.
    rules = [r for r in _permissions()["deny"] if r.startswith("mcp__questrade")]
    unknown = sorted(r for r in rules if r.removeprefix(PREFIX) not in MUTATING)
    assert not unknown, f"unexpected Questrade deny rules: {unknown}"


def test_classification_is_consistent() -> None:
    assert not (MUTATING & READ_ONLY)
    # Only get_/list_/search_ verbs may be classed read-only; anything else
    # (create_, preview_, edit_, update_, delete_, cancel_, ...) must be denied.
    assert all(READ_VERB.match(t) for t in READ_ONLY)
    assert not any(READ_VERB.match(t) for t in MUTATING)
