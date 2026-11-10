"""CI shape: gitleaks always runs, backend jobs are path-filtered, live tests can never run, every job
is read-only, and gitleaks uses the same digest-pinned image as the pre-commit hook."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import pytest_collection_modifyitems
from tests.unit.test_publish_guard import job_permissions

REPO = Path(__file__).resolve().parents[3]
CI = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
HOOK = (REPO / ".githooks" / "pre-commit").read_text(encoding="utf-8")
BACKEND_JOBS = ("ruff", "mypy", "pytest")


def job_text(name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", CI, re.M | re.S)
    assert m, f"job {name} not found"
    return m.group(1)


def test_triggers_are_pull_requests_and_pushes_to_main():
    assert re.search(r"^on:\n  pull_request:\n  push:\n    branches: \[main\]\n", CI, re.M)


def test_every_job_is_read_only():
    perms = job_permissions(CI)
    assert set(perms) == {"changes", "gitleaks", *BACKEND_JOBS}
    assert all(p == {"contents: read"} for p in perms.values()), perms


def test_gitleaks_always_runs_over_history_with_the_hooks_image():
    gl = job_text("gitleaks")
    assert "if:" not in gl.split("steps:")[0], "gitleaks must not be path-filtered"
    assert "fetch-depth: 0" in gl
    hook_image = re.search(r'IMAGE="([^"]+)"', HOOK).group(1)  # type: ignore[union-attr]
    ci_image = re.search(r"GITLEAKS_IMAGE: (\S+)", CI).group(1)  # type: ignore[union-attr]
    assert ci_image == hook_image and "@sha256:" in ci_image


@pytest.mark.parametrize("job", BACKEND_JOBS)
def test_backend_jobs_are_path_filtered_and_use_locked_cached_uv(job):
    t = job_text(job)
    assert "needs: changes" in t
    assert "if: needs.changes.outputs.backend == 'true'" in t
    assert "enable-cache: true" in t and "cache-dependency-glob: backend/uv.lock" in t
    assert "uv sync --locked" in t


def test_path_filter_covers_backend_and_shared_config():
    pattern = re.search(r"grep -qE '([^']+)'", job_text("changes")).group(1)  # type: ignore[union-attr]
    rx = re.compile(pattern)
    for path in (
        "backend/app/cli.py",
        "deploy/static/oauth/client-metadata.json",
        "docs/spikes/x.md",
        ".github/workflows/ci.yml",
        "Caddyfile",
        ".githooks/pre-commit",
    ):
        assert rx.search(path), path
    for path in ("mobile/App.tsx", "README.md"):
        assert not rx.search(path), path


def test_live_tests_can_never_run_in_ci():
    t = job_text("pytest")
    assert 'pytest -m "not live"' in t
    assert 'AURELIUS_LIVE_TESTS: "0"' in t
    assert "secrets." not in CI, "CI must not receive any secret (no live credentials can exist there)"


def test_python_version_comes_from_pyproject_not_the_workflow():
    assert "python-version" not in CI


@pytest.mark.parametrize(
    ("env", "skipped"),
    [
        ({}, True),
        ({"AURELIUS_LIVE_TESTS": "0"}, True),
        ({"AURELIUS_LIVE_TESTS": "1", "CI": "true"}, True),  # even if someone sets it in CI
        ({"AURELIUS_LIVE_TESTS": "1"}, False),  # only locally, on purpose
    ],
)
def test_live_marker_skip_rules(monkeypatch, env, skipped):
    for k in ("AURELIUS_LIVE_TESTS", "CI"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    added: list[object] = []
    item = SimpleNamespace(keywords={"live": True}, add_marker=added.append)
    other = SimpleNamespace(keywords={}, add_marker=added.append)
    pytest_collection_modifyitems(None, [item, other])  # type: ignore[arg-type]
    assert (len(added) == 1) is skipped
