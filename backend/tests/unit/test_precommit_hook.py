"""The gitleaks pre-commit hook must scan the index git is actually committing.

`git commit -a` and `git commit -- <path>` build a temporary index and announce it in
GIT_INDEX_FILE. Before 2026-11-10 the Docker path ignored it, read .git/index, and scanned
nothing ("0 commits scanned"). Live-verified scenarios (fresh commit, amend, -a, -- <path>,
amend -a, staged-but-cleaned key; plus benign deletion, binary, rename and message-only amend)
are recorded in the commit that fixed it. This test keeps the two defences from being removed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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


# -- which gitleaks runs (local binary only at the pinned version) ----------------------------------

PIN_RE = re.compile(r'^PINNED_VERSION="([0-9.]+)"$', re.MULTILINE)
CI = HOOK.parents[1] / ".github" / "workflows" / "ci.yml"


def test_pinned_version_matches_the_image_and_ci() -> None:
    text = _hook()
    match = PIN_RE.search(text)
    assert match, "hook must declare PINNED_VERSION"
    version = match.group(1)
    assert f"gitleaks:v{version}@sha256:" in text, "IMAGE tag must match PINNED_VERSION"
    assert f"gitleaks:v{version}@sha256:" in CI.read_text(encoding="utf-8"), "ci.yml must run the same version"


FAKE_GITLEAKS = """#!/bin/sh
if [ "$1" = version ]; then echo "{version}"; exit 0; fi
echo native >> "$(dirname "$0")/calls"
echo "1 commits scanned."
"""
FAKE_DOCKER = """#!/bin/sh
if [ "$1" = info ]; then exit {info_rc}; fi
echo docker >> "$(dirname "$0")/calls"
echo "1 commits scanned."
"""

SH = shutil.which("sh")


def run_hook(tmp_path: Path, gitleaks_version: str | None, docker_up: bool) -> tuple[int, str, str]:
    """Run the real hook in a scratch repo with fake gitleaks/docker first on PATH. Returns (rc, stderr, calls)."""
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    if gitleaks_version is not None:
        (fakes / "gitleaks").write_bytes(FAKE_GITLEAKS.format(version=gitleaks_version).encode())
    (fakes / "docker").write_bytes(FAKE_DOCKER.format(info_rc=0 if docker_up else 1).encode())
    for f in fakes.iterdir():
        f.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}"}
    env.pop("GIT_INDEX_FILE", None)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)  # noqa: S607
    (repo / "note.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "note.txt"], cwd=repo, check=True, env=env)  # noqa: S607
    assert SH is not None
    result = subprocess.run(  # noqa: S603 - our own hook under sh
        [SH, str(HOOK)], cwd=repo, env=env, capture_output=True, text=True, check=False
    )
    calls = fakes / "calls"
    return result.returncode, result.stderr, calls.read_text() if calls.exists() else ""


needs_sh = pytest.mark.skipif(SH is None, reason="no POSIX sh")


@needs_sh
@pytest.mark.parametrize("reported", ["8.21.2", "v8.21.2"])
def test_matching_local_gitleaks_is_used(tmp_path: Path, reported: str) -> None:
    rc, _, calls = run_hook(tmp_path, reported, docker_up=False)
    assert (rc, calls) == (0, "native\n")


@needs_sh
def test_other_local_version_falls_back_to_the_pinned_image(tmp_path: Path) -> None:
    rc, err, calls = run_hook(tmp_path, "8.24.0", docker_up=True)
    assert (rc, calls) == (0, "docker\n")
    assert "not 8.21.2; using the pinned image" in err


@needs_sh
def test_other_local_version_without_docker_is_blocked(tmp_path: Path) -> None:
    rc, err, calls = run_hook(tmp_path, "8.24.0", docker_up=False)
    assert rc != 0 and calls == ""
    assert "BLOCKED" in err and "version 8.24.0, not the pinned 8.21.2" in err


@needs_sh
@pytest.mark.skipif(shutil.which("gitleaks") is not None, reason="a real gitleaks is on PATH")
def test_no_gitleaks_and_no_docker_is_blocked(tmp_path: Path) -> None:
    rc, err, calls = run_hook(tmp_path, None, docker_up=False)
    assert rc != 0 and calls == ""
    assert "gitleaks is unavailable" in err
