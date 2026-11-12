"""Postgres for the database tests (decision P28).

Where the server comes from, in order:
1. ``AURELIUS_TEST_DATABASE_URL``: an admin URL (any database, usually ``postgres``) on a server the
   tests may create and drop databases on. CI sets it to its Postgres 16 service container.
2. Otherwise, if Docker is running, the pinned ``PG_IMAGE`` is started for the session and removed after.
3. Otherwise the tests skip, unless ``AURELIUS_REQUIRE_DATABASE=1`` (set in CI), which makes them fail.

The schema is built once by the real migrations into a template database; each test gets a fresh
copy (``CREATE DATABASE ... TEMPLATE``), so tests are isolated and the triggers are the migrated ones.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.db.backup import PG_IMAGE
from app.db.migrate import upgrade
from app.db.session import parse_url, session_factory

URL_ENV = "AURELIUS_TEST_DATABASE_URL"
REQUIRE_ENV = "AURELIUS_REQUIRE_DATABASE"
DOCKER = shutil.which("docker")


def _unavailable(reason: str) -> None:
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"database tests are required here ({REQUIRE_ENV}=1) but {reason}")
    pytest.skip(f"no test database: {reason}")


def _docker_running() -> bool:
    if DOCKER is None:
        return False
    probe = subprocess.run([DOCKER, "info"], capture_output=True, check=False, stdin=subprocess.DEVNULL)
    return probe.returncode == 0


def _start_container() -> tuple[str, URL]:
    assert DOCKER is not None
    name = f"aurelius-test-pg-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            DOCKER, "run", "-d", "--rm", "--name", name,
            "-e", "POSTGRES_USER=aurelius", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
            "-p", "127.0.0.1::5432", PG_IMAGE,
        ],
        check=True, capture_output=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    port = (
        subprocess.run(
            [DOCKER, "port", name, "5432/tcp"], check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
        .stdout.split(":")[-1]
        .strip()
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready = subprocess.run(
            [DOCKER, "exec", name, "pg_isready", "-U", "aurelius", "-h", "127.0.0.1"],
            capture_output=True, check=False, stdin=subprocess.DEVNULL,
        )  # fmt: skip
        if ready.returncode == 0:
            break
        time.sleep(0.5)
    else:
        subprocess.run([DOCKER, "rm", "-f", name], capture_output=True, check=False, stdin=subprocess.DEVNULL)
        pytest.fail("the Postgres test container did not become ready")
    return name, parse_url(f"postgresql://aurelius@127.0.0.1:{port}/postgres")


@pytest.fixture(scope="session")
def admin_url() -> Iterator[URL]:
    raw = os.environ.get(URL_ENV)
    if raw:
        yield parse_url(raw)
        return
    if not _docker_running():
        _unavailable(f"{URL_ENV} is unset and Docker is not running")
    name, url = _start_container()
    try:
        yield url
    finally:
        assert DOCKER is not None
        subprocess.run([DOCKER, "rm", "-f", name], capture_output=True, check=False, stdin=subprocess.DEVNULL)


def _admin_engine(url: URL) -> AsyncEngine:
    return create_async_engine(url, isolation_level="AUTOCOMMIT")


async def create_database(admin: URL, name: str, template: str | None = None) -> URL:
    engine = _admin_engine(admin)
    try:
        async with engine.connect() as conn:
            clause = f' TEMPLATE "{template}"' if template else ""
            await conn.execute(text(f'CREATE DATABASE "{name}"{clause}'))
    finally:
        await engine.dispose()
    return admin.set(database=name)


async def drop_database(admin: URL, name: str) -> None:
    engine = _admin_engine(admin)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def template_db(admin_url: URL) -> Iterator[str]:
    name = f"aurelius_tpl_{uuid.uuid4().hex[:8]}"

    async def build() -> None:
        url = await create_database(admin_url, name)
        engine = create_async_engine(url)
        try:
            await upgrade(engine)
        finally:
            await engine.dispose()

    asyncio.run(build())
    try:
        yield name
    finally:
        asyncio.run(drop_database(admin_url, name))


@pytest.fixture
async def db_url(admin_url: URL, template_db: str) -> AsyncIterator[URL]:
    name = f"aurelius_t_{uuid.uuid4().hex[:12]}"
    url = await create_database(admin_url, name, template=template_db)
    try:
        yield url
    finally:
        await drop_database(admin_url, name)


@pytest.fixture
async def engine(db_url: URL) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(db_url)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with session_factory(engine)() as s:
        yield s
