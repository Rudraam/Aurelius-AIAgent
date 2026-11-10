"""The gitleaks pre-commit hook must scan the index git is actually committing.

`git commit -a` and `git commit -- <path>` build a temporary index and announce it in
GIT_INDEX_FILE. Before 2026-11-10 the Docker path ignored it, read .git/index, and scanned
nothing ("0 commits scanned"). Live-verified scenarios (fresh commit, amend, -a, -- <path>,
amend -a, staged-but-cleaned key; plus benign deletion, binary, rename and message-only amend)
are recorded in the commit that fixed it. This test keeps the two defences from being removed.
"""

from __future__ import annotations

from pathlib import Path

HOOK = Path(__file__).resolve().parents[3] / ".githooks" / "pre-commit"


def _hook() -> str:
    return HOOK.read_text(encoding="utf-8")


def test_hook_hands_the_commit_index_to_docker() -> None:
    text = _hook()
    assert "index=${GIT_INDEX_FILE:-" in text, "hook must start from git's GIT_INDEX_FILE"
    assert 'cp "$index"' in text, "hook must snapshot the index being committed"
    assert "-e GIT_INDEX_FILE=/idx/index" in text, "container must scan the snapshot, not .git/index"


def test_hook_fails_closed_on_an_empty_scan() -> None:
    text = _hook()
    assert "git diff --cached --numstat" in text
    assert "commits? scanned" in text, "hook must check gitleaks actually scanned the change"


def test_hook_fails_closed_without_gitleaks() -> None:
    assert "gitleaks is unavailable" in _hook()


def test_hook_has_lf_line_endings() -> None:
    assert b"\r\n" not in HOOK.read_bytes(), "a CRLF hook breaks under sh"
