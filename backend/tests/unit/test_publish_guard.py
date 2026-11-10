"""What GitHub Pages publishes is fixed: deploy/static holds exactly the allowlisted files, and the
Pages workflow uploads exactly deploy/static. Every workflow pins actions to full commit SHAs and grants
permissions per job only.

Adding a file under deploy/static fails this test (and the Pages workflow's own guard step), so the
build fails. To publish something new, change PUBLISHED_ALLOWLIST here *and* the workflow guard, in
a reviewed commit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
STATIC = REPO / "deploy" / "static"
WORKFLOWS = REPO / ".github" / "workflows"
PAGES = WORKFLOWS / "pages.yml"

PUBLISHED_ALLOWLIST = frozenset({"oauth/client-metadata.json"})

USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<ref>\S+?)@(?P<pin>\S+)(?P<comment>\s+#.*)?$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def published_files(root: Path) -> set[str]:
    """Every file or symlink under ``root``, hidden ones included, as POSIX paths relative to it."""
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}


def workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def step_block(text: str, uses_prefix: str) -> list[str]:
    """Lines of the single workflow step whose ``uses:`` starts with ``uses_prefix``."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if re.search(rf"uses:\s*{re.escape(uses_prefix)}@", ln)]
    assert len(starts) == 1, f"expected exactly one {uses_prefix} step, found {len(starts)}"
    i = starts[0]
    indent = len(lines[i]) - len(lines[i].lstrip(" -"))
    block = [lines[i]]
    for ln in lines[i + 1 :]:
        stripped = ln.lstrip(" ")
        if stripped.startswith("- ") and len(ln) - len(stripped) < indent:
            break
        if stripped and len(ln) - len(stripped) < indent - 2:
            break
        block.append(ln)
    return block


def job_permissions(text: str) -> dict[str, set[str]]:
    """Map job name -> set of 'scope: level' lines from its job-level permissions block."""
    lines = text.splitlines()
    jobs: dict[str, set[str]] = {}
    in_jobs, job, in_perms = False, None, False
    for ln in lines:
        if ln.startswith("jobs:"):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", ln):
            job, in_perms = ln.strip().rstrip(":"), False
            jobs[job] = set()
            continue
        if job and re.match(r"^    permissions:\s*$", ln):
            in_perms = True
            continue
        if job and re.match(r"^    permissions:\s*\{(.*)\}\s*$", ln):
            inner = re.match(r"^    permissions:\s*\{(.*)\}\s*$", ln).group(1)  # type: ignore[union-attr]
            jobs[job] = {p.strip() for p in inner.split(",") if p.strip()}
            continue
        if in_perms:
            m = re.match(r"^      ([a-z-]+:\s*[a-z]+)\s*$", ln)
            if m:
                jobs[job].add(re.sub(r"\s+", " ", m.group(1)))  # type: ignore[index]
            else:
                in_perms = False
    return jobs


# --------------------------------------------------------------------------- deploy/static allowlist


def test_deploy_static_contains_exactly_the_allowlist():
    assert published_files(STATIC) == set(PUBLISHED_ALLOWLIST)


def test_published_files_sees_hidden_and_nested_files(tmp_path):
    """Guard the guard: an extra file, hidden file (e.g. CNAME, .env) or nested file is detected."""
    (tmp_path / "oauth").mkdir()
    (tmp_path / "oauth" / "client-metadata.json").write_text("{}")
    assert published_files(tmp_path) == set(PUBLISHED_ALLOWLIST)
    for extra in ("CNAME", ".env", "index.html", "oauth/.secret", "deep/er/file.txt"):
        target = tmp_path / extra
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
        assert published_files(tmp_path) != set(PUBLISHED_ALLOWLIST), extra
        target.unlink()


def test_no_cname_file_anywhere_in_published_tree():
    assert not any(p.name == "CNAME" for p in STATIC.rglob("*"))


# --------------------------------------------------------------------------- pages.yml


def test_pages_uploads_exactly_deploy_static():
    block = step_block(PAGES.read_text(encoding="utf-8"), "actions/upload-pages-artifact")
    paths = [ln.split(":", 1)[1].strip().strip("\"'") for ln in block if re.match(r"^\s*path:", ln)]
    assert paths == ["deploy/static"]


def test_pages_workflow_guard_matches_allowlist():
    text = PAGES.read_text(encoding="utf-8")
    m = re.search(r'expected="([^"]*)"', text)
    assert m is not None, "the Pages workflow must keep its in-workflow allowlist guard"
    assert set(m.group(1).split()) == set(PUBLISHED_ALLOWLIST)


def test_pages_triggers_permissions_and_concurrency():
    text = PAGES.read_text(encoding="utf-8")
    assert re.search(r"^  push:\n    branches: \[main\]\n    paths:\n", text, re.M)
    assert '      - "deploy/static/**"' in text
    assert '      - ".github/workflows/pages.yml"' in text
    assert re.search(r"^  workflow_dispatch:\s*$", text, re.M)
    assert re.search(r"^concurrency:\n  group: pages\n  cancel-in-progress: false\s*$", text, re.M)
    assert job_permissions(text) == {"deploy": {"contents: read", "pages: write", "id-token: write"}}


# --------------------------------------------------------------------------- all workflows


@pytest.mark.parametrize("wf", workflow_files(), ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_sha_with_version_comment(wf):
    problems = []
    for lineno, ln in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
        m = USES_RE.match(ln)
        if not m:
            continue
        if m.group("ref").startswith("./"):
            continue  # local action: versioned with this repo
        if not SHA_RE.match(m.group("pin")):
            problems.append(f"line {lineno}: {m.group('ref')} pinned to {m.group('pin')!r}, not a commit SHA")
        if not (m.group("comment") or "").strip().startswith("# v"):
            problems.append(f"line {lineno}: {m.group('ref')} lacks a '# vX.Y.Z' comment")
    assert problems == []


@pytest.mark.parametrize("wf", workflow_files(), ids=lambda p: p.name)
def test_permissions_are_job_level_only(wf):
    text = wf.read_text(encoding="utf-8")
    top = re.findall(r"^permissions:.*$", text, re.M)
    assert top == ["permissions: {}"], "workflow-level permissions must be exactly {} (grant per job)"
    perms = job_permissions(text)
    assert perms, "no jobs found"
    assert all(perms.values()), f"every job must declare its own permissions: {perms}"


def test_guard_parsers_catch_unpinned_and_tag_refs():
    assert USES_RE.match("      - uses: actions/checkout@v4")
    assert not SHA_RE.match(USES_RE.match("      - uses: actions/checkout@v4").group("pin"))  # type: ignore[union-attr]
    pinned = "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"
    m = USES_RE.match(pinned)
    assert m and SHA_RE.match(m.group("pin")) and m.group("comment").strip() == "# v7.0.1"
