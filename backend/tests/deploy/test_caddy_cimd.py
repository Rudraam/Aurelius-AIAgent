"""Run the real Caddyfile in the pinned Caddy image and check how the CIMD document is served.

The site address is overridden to ``:8080`` (plain HTTP, no ACME) so the test needs neither DNS nor
certificates. The ``handle /oauth/client-metadata.json`` block under test is identical for the real
domain. The mount at ``/srv/static`` is the contract docker-compose (M1) must follow.
Skipped when Docker is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

REPO = Path(__file__).resolve().parents[3]
CADDY_IMAGE = "caddy:2.10.2"
CLIENT_ID = "https://aurelius.rivenpath.ai/oauth/client-metadata.json"
DOC = REPO / "deploy" / "static" / "oauth" / "client-metadata.json"

pytestmark = pytest.mark.docker


DOCKER = shutil.which("docker")  # absolute path; every argument below is a constant or generated here


def _docker_available() -> bool:
    if DOCKER is None:
        return False
    return subprocess.run([DOCKER, "info"], capture_output=True, stdin=subprocess.DEVNULL).returncode == 0


@pytest.fixture(scope="module")
def caddy_url() -> Iterator[str]:
    if not _docker_available():
        if os.environ.get("AURELIUS_REQUIRE_DOCKER") == "1":
            pytest.fail("docker not available but AURELIUS_REQUIRE_DOCKER=1 (CI must run the deploy tests)")
        pytest.skip("docker not available")
    assert DOCKER is not None
    name = f"aurelius-caddy-test-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [
            DOCKER,
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "AURELIUS_DOMAIN=:8080",
            "-p",
            "127.0.0.1::8080",
            "-v",
            f"{(REPO / 'Caddyfile').as_posix()}:/etc/caddy/Caddyfile:ro",
            "-v",
            f"{(REPO / 'deploy' / 'static').as_posix()}:/srv/static:ro",
            CADDY_IMAGE,
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert run.returncode == 0, run.stderr
    try:
        port = (
            subprocess.run([DOCKER, "port", name, "8080/tcp"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
            .stdout.strip()
            .splitlines()[0]
            .rsplit(":", 1)[1]
        )
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx2.get(f"{url}/oauth/client-metadata.json", timeout=2)
                break
            except httpx2.HTTPError:
                if time.monotonic() > deadline:
                    logs = subprocess.run([DOCKER, "logs", name], capture_output=True, text=True).stderr
                    pytest.fail(f"caddy did not come up:\n{logs[-2000:]}")
                time.sleep(0.3)
        yield url
    finally:
        subprocess.run([DOCKER, "rm", "-f", name], capture_output=True, stdin=subprocess.DEVNULL)


def test_caddy_serves_cimd_as_application_json(caddy_url: str) -> None:
    r = httpx2.get(f"{caddy_url}/oauth/client-metadata.json", timeout=5)
    assert r.status_code == 200
    content_types = r.headers.get_list("content-type")
    assert content_types == ["application/json"], content_types


def test_caddy_serves_the_committed_document_with_the_permanent_client_id(caddy_url: str) -> None:
    r = httpx2.get(f"{caddy_url}/oauth/client-metadata.json", timeout=5)
    served = r.json()
    assert served["client_id"] == CLIENT_ID
    assert served == json.loads(DOC.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", ["/oauth/client-metadata.json.bak", "/oauth/", "/client-metadata.json"])
def test_only_the_exact_cimd_path_is_static(caddy_url: str, path: str) -> None:
    """Every other path must be routed to the API, not the static file server.

    The API upstream is absent in this test, so a correctly routed request gets 502 from the reverse
    proxy. A 404 here would mean the static handler matched, i.e. it was widened beyond the one file.
    """
    r = httpx2.get(f"{caddy_url}{path}", timeout=10)
    assert r.status_code == 502, f"{path} -> {r.status_code} (expected the reverse proxy, not static)"
