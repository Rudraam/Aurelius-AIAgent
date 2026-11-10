"""Supply-chain policy: no dependency or action version younger than 14 days.

Enforced twice for Python: Dependabot's cooldown, and uv itself (``exclude-newer = "14 days"``),
which the lockfile records as a relative span, so ``uv sync --locked`` stays stable over time.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

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
    for eco in ("github-actions", "uv", "docker"):
        b = ecosystem_block(eco)
        assert re.search(r"cooldown:\n      default-days: 14\n", b), eco
        assert "interval: weekly" in b, eco
        assert re.search(r"applies-to: version-updates\n\s+update-types: \[minor, patch\]", b), eco


def test_uv_refuses_versions_younger_than_14_days():
    cfg = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    assert cfg["tool"]["uv"]["exclude-newer"] == "14 days"
    lock = (BACKEND / "uv.lock").read_text(encoding="utf-8")
    assert 'exclude-newer-span = "P14D"' in lock, "uv.lock must be resolved under the 14-day span (run `uv lock`)"


# --------------------------------------------------------------------------- container images: digest pins only

DIGEST = r"@sha256:[0-9a-f]{64}"
PINNED_REF = re.compile(rf"^\S+{DIGEST}$")
# Registries and official base images we use or are likely to use. A reference to any of these must be pinned.
IMAGE_TOKEN = re.compile(
    r"(?<![\w./-])("
    r"(?:docker\.io|ghcr\.io|quay\.io|gcr\.io|public\.ecr\.aws|mcr\.microsoft\.com)/[\w./-]+(?::[\w.-]+)?"
    r"|(?:python|caddy|postgres|node|alpine|debian|ubuntu|redis|nginx|busybox)(?::[\w.-]+)"
    r")(?P<digest>@sha256:[0-9a-f]{64})?"
)
ASSIGNMENT = re.compile(r"""\b\w*IMAGE\w*\s*[:=]\s*["']?([^"'\s]+)""")
SKIP_SUFFIXES = {".md", ".lock", ".png", ".jpg", ".ico"}


def is_dockerfile(p: Path) -> bool:
    return p.name == "Dockerfile" or p.name.startswith("Dockerfile.") or p.name.endswith(".Dockerfile")


def image_problems(path_name: str, text: str, dockerfile: bool) -> list[str]:
    problems: list[str] = []
    stages: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        where = f"{path_name}:{lineno}"
        if dockerfile:
            if re.match(r"^#\s*syntax\s*=", line, re.I):
                problems.append(f"{where}: '# syntax=' pulls an unpinned frontend image")
            m = re.match(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line, re.I)
            if m:
                if m.group(1) not in stages and not PINNED_REF.match(m.group(1)):
                    problems.append(f"{where}: FROM {m.group(1)} is not digest-pinned")
                if m.group(2):
                    stages.add(m.group(2))
            m = re.search(r"--from=(\S+)", line)
            if m and m.group(1) not in stages and not m.group(1).isdigit() and not PINNED_REF.match(m.group(1)):
                problems.append(f"{where}: COPY --from={m.group(1)} is not digest-pinned")
            continue
        if line.startswith("#") and "IMAGE" not in line:
            continue
        for m in ASSIGNMENT.finditer(raw):
            value = m.group(1)
            if (":" in value or "/" in value) and not value.startswith("$") and not PINNED_REF.match(value):
                problems.append(f"{where}: image assignment {value!r} is not digest-pinned")
        for m in re.finditer(r"(?:^|\s)image:\s*[\"']?([^\"'\s]+)", raw):
            if not PINNED_REF.match(m.group(1)):
                problems.append(f"{where}: image: {m.group(1)!r} is not digest-pinned")
        for m in re.finditer(r"uses:\s*docker://(\S+)", raw):
            if not PINNED_REF.match(m.group(1)):
                problems.append(f"{where}: docker:// action {m.group(1)!r} is not digest-pinned")
        for m in IMAGE_TOKEN.finditer(raw):
            if not m.group("digest") and ":" in m.group(1).split("/")[-1]:
                problems.append(f"{where}: image reference {m.group(1)!r} is not digest-pinned")
    return problems


GIT = shutil.which("git") or "git"


def repo_files() -> list[Path]:
    out = subprocess.run(
        [GIT, "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        stdin=subprocess.DEVNULL,
    ).stdout.splitlines()
    files = []
    for rel in out:
        p = REPO / rel
        if not p.is_file() or rel.startswith("docs/") or p.suffix in SKIP_SUFFIXES:
            continue
        if p.resolve() == Path(__file__).resolve():
            continue  # this file holds deliberately bad examples
        files.append(p)
    return files


def test_every_container_image_reference_is_digest_pinned():
    problems: list[str] = []
    for p in repo_files():
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        problems += image_problems(p.relative_to(REPO).as_posix(), text, is_dockerfile(p))
    assert problems == []


def test_known_image_sites_exist_and_are_pinned():
    """The scan must actually see our image references (guards against it silently matching nothing)."""
    backend_df = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    caddy_df = (REPO / "deploy" / "caddy" / "Dockerfile").read_text(encoding="utf-8")
    hook = (REPO / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    assert re.search(rf"^FROM python:3\.12-slim-bookworm{DIGEST}$", backend_df, re.M)
    assert re.search(rf"--from=ghcr\.io/astral-sh/uv:0\.12\.5{DIGEST} ", backend_df)
    assert re.search(rf"^FROM caddy:2\.10\.2{DIGEST}$", caddy_df, re.M)
    assert re.search(rf'IMAGE="ghcr\.io/gitleaks/gitleaks:v8\.21\.2{DIGEST}"', hook)


@pytest.mark.parametrize(
    ("name", "text", "dockerfile"),
    [
        ("Dockerfile", "FROM python:3.12-slim-bookworm\n", True),
        ("Dockerfile", "COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv\n", True),
        ("Dockerfile", "# syntax=docker/dockerfile:1.7\nFROM scratch@sha256:" + "0" * 64 + "\n", True),
        ("hook", 'IMAGE="ghcr.io/gitleaks/gitleaks:v8.21.2"\n', False),
        ("test.py", 'CADDY_IMAGE = "caddy:2.10.2"\n', False),
        ("compose.yml", "    image: postgres:16\n", False),
        ("ci.yml", "      - uses: docker://alpine:3.20\n", False),
        ("script.py", 'subprocess.run(["docker", "run", "--rm", "redis:7.4"])\n', False),
    ],
)
def test_guard_flags_unpinned_images(name, text, dockerfile):
    assert image_problems(name, text, dockerfile), text


@pytest.mark.parametrize(
    ("name", "text", "dockerfile"),
    [
        (
            "Dockerfile",
            "FROM python:3.12-slim-bookworm@sha256:" + "a" * 64 + " AS base\nFROM base\nCOPY --from=base /x /x\n",
            True,
        ),
        ("hook", 'IMAGE="ghcr.io/gitleaks/gitleaks:v8.21.2@sha256:' + "b" * 64 + '"\n', False),
        ("auth.py", '    "mcp:brokerage.accounts.read",\n    "mcp:read",\n', False),  # OAuth scopes, not images
        ("ci.yml", "  GITLEAKS_IMAGE: ghcr.io/gitleaks/gitleaks:v8.21.2@sha256:" + "c" * 64 + "\n", False),
        ("test.py", '        "docker", "run", "-v", f"{root}:/repo:ro", IMAGE,\n', False),
    ],
)
def test_guard_accepts_pinned_images_and_non_images(name, text, dockerfile):
    assert image_problems(name, text, dockerfile) == []


def test_dependabot_covers_docker_with_cooldown():
    b = ecosystem_block("docker")
    assert "      - /backend\n" in b and "      - /deploy/caddy\n" in b
    assert re.search(r"cooldown:\n      default-days: 14\n", b)
    assert "interval: weekly" in b
