"""Claude Code's shell deny list: gh guardrails present, and Bash/PowerShell mirrored exactly.

The deny list in ``.claude/settings.json`` is generated from one pattern list and
emitted once as ``Bash(...)`` and once as ``PowerShell(...)``. A rule present in
only one form is a hole in the other shell. The gh rules keep Claude Code away
from raw API calls, repo/ruleset/secret administration, release deletion and
admin-override merges (decision of 2026-11-10). Each was verified with a
harmless ``--help`` probe in both shells.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SETTINGS = REPO / ".claude" / "settings.json"

REQUIRED_GH = frozenset(
    {
        "gh api*",
        "gh repo delete*",
        "gh repo edit*",
        "gh secret*",
        "gh release delete*",
        "gh pr merge --admin*",
        "gh pr merge * --admin*",
        "gh ruleset*",
    }
)


def _shell_patterns() -> tuple[list[str], list[str]]:
    deny: list[str] = json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"]["deny"]
    bash = [r[len("Bash(") : -1] for r in deny if r.startswith("Bash(")]
    pwsh = [r[len("PowerShell(") : -1] for r in deny if r.startswith("PowerShell(")]
    return bash, pwsh


def test_bash_and_powershell_denies_mirror_each_other() -> None:
    bash, pwsh = _shell_patterns()
    assert bash == pwsh, f"only Bash: {sorted(set(bash) - set(pwsh))}; only PowerShell: {sorted(set(pwsh) - set(bash))}"


def test_gh_guardrails_are_denied_in_both_shells() -> None:
    bash, pwsh = _shell_patterns()
    for name, patterns in (("Bash", bash), ("PowerShell", pwsh)):
        missing = sorted(REQUIRED_GH - set(patterns))
        assert not missing, f"{name} gh denies missing: {missing}"


def test_no_gh_rule_is_allowed_or_askable() -> None:
    perms = json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"]
    for key in ("allow", "ask"):
        hits = [r for r in perms.get(key, []) if "(gh " in r]
        assert not hits, f"gh rules in permissions.{key}: {hits}"
