"""``aurelius db backup`` / ``aurelius db restore`` (decision P28).

A backup is ``pg_dump --format=custom`` written to ``backend/.local/backups/`` (gitignored) as
``aurelius-<UTC timestamp>.dump``. A restore refuses a database that already has tables unless
``force`` is set (then the dump's objects are dropped and recreated). Either way the restore runs in a
single transaction and stops at the first error.

pg_dump and pg_restore must match the server's major version. A local binary (from
``AURELIUS_PG_BIN`` or PATH) is used only if its major version equals the server's. Otherwise, for a
server of the pinned image's major version, the tools run from ``PG_IMAGE`` under Docker. The password
reaches the tools only through the ``PGPASSWORD`` environment variable, never the command line.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

#: The one Postgres image (compose, CI and the test fixture must match; test_db_backup.py checks).
# postgres 16.14 (bookworm), index digest built 2026-08-05.
PG_IMAGE = "postgres:16.14-bookworm@sha256:64154d0babcb1741988719e703419af0382b19953706149f9872fbd0f438efa8"
PG_BIN_ENV = "AURELIUS_PG_BIN"
DEFAULT_BACKUP_DIR = Path(__file__).resolve().parents[2] / ".local" / "backups"
DOCKER_HOST_ALIAS = "host.docker.internal"
_VERSION = re.compile(r"\(PostgreSQL\)\s+(\d+)")


class BackupError(RuntimeError):
    pass


def image_major() -> int:
    tag = PG_IMAGE.split("@", 1)[0].split(":", 1)[1]
    return int(tag.split(".", 1)[0].split("-", 1)[0])


@dataclass(frozen=True)
class PgCommand:
    """How to run one Postgres client tool, and the connection arguments to give it."""

    argv: list[str]
    via: str  # "native" or "docker"
    env: dict[str, str] = field(default_factory=dict, repr=False)


def _binary_major(path: str) -> int | None:
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, check=False, timeout=30, stdin=subprocess.DEVNULL
        ).stdout
    except OSError:
        return None
    match = _VERSION.search(out)
    return int(match.group(1)) if match else None


def native_tool(tool: str, major: int) -> str | None:
    """A local ``tool`` whose major version is ``major``, from AURELIUS_PG_BIN first, then PATH."""
    candidates: list[str] = []
    pg_bin = os.environ.get(PG_BIN_ENV)
    if pg_bin:
        for name in (tool, f"{tool}.exe"):
            path = Path(pg_bin) / name
            if path.is_file():
                candidates.append(str(path))
    found = shutil.which(tool)
    if found:
        candidates.append(found)
    for candidate in candidates:
        if _binary_major(candidate) == major:
            return candidate
    return None


def docker_running() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    probe = subprocess.run([docker, "info"], capture_output=True, check=False, stdin=subprocess.DEVNULL, timeout=60)
    return probe.returncode == 0


def _connection_args(url: URL, *, docker: bool, platform: str) -> list[str]:
    host = url.host
    if docker and platform != "linux" and host in (None, "localhost", "127.0.0.1", "::1"):
        host = DOCKER_HOST_ALIAS  # Docker Desktop: the host's loopback, seen from a container
    args: list[str] = []
    if host:
        args += ["--host", host]
    if url.port:
        args += ["--port", str(url.port)]
    if url.username:
        args += ["--username", url.username]
    assert url.database
    args += ["--dbname", url.database]
    return args


def build_command(
    tool: str, url: URL, *, native: str | None, docker: str | None, server_major: int, platform: str = sys.platform
) -> PgCommand:
    """The command for ``tool`` against ``url``: native if it matches the server, else the pinned image."""
    env = {"PGPASSWORD": url.password} if url.password else {}
    if native is not None:
        return PgCommand([native, *_connection_args(url, docker=False, platform=platform)], "native", env)
    if docker is not None and server_major == image_major():
        argv = [docker, "run", "--rm", "-i"]
        if env:
            argv += ["-e", "PGPASSWORD"]  # the value comes from the environment, not argv
        if platform == "linux":
            argv += ["--network", "host"]
        argv += [PG_IMAGE, tool, *_connection_args(url, docker=True, platform=platform)]
        return PgCommand(argv, "docker", env)
    raise BackupError(
        f"{tool} for PostgreSQL {server_major} is not available: put it on PATH or set {PG_BIN_ENV} to its"
        f" directory"
        + (f", or start Docker (the pinned image is PostgreSQL {image_major()})" if docker is None else "")
    )


async def server_major(url: URL) -> int:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            num = int((await conn.execute(text("SHOW server_version_num"))).scalar_one())
    finally:
        await engine.dispose()
    return num // 10000


async def database_is_empty(url: URL) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                        " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')"
                    )
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    return int(count) == 0


def resolve(tool: str, url: URL, major: int) -> PgCommand:
    docker = shutil.which("docker") if docker_running() else None
    return build_command(tool, url, native=native_tool(tool, major), docker=docker, server_major=major)


def _run(command: PgCommand, extra: list[str], **kwargs: object) -> None:
    env = {**os.environ, **command.env}
    if command.via == "docker":
        # Tool options go after the tool name, i.e. at the end of argv.
        argv = [*command.argv, *extra]
    else:
        argv = [command.argv[0], *extra, *command.argv[1:]]
    result = subprocess.run(argv, env=env, check=False, stderr=subprocess.PIPE, **kwargs)  # type: ignore[call-overload]
    if result.returncode != 0:
        raise BackupError(
            f"{Path(argv[0]).name} failed ({result.returncode}): {result.stderr.decode(errors='replace')}"
        )


def backup(url: URL, major: int, out_dir: Path, now: datetime) -> Path:
    """Dump ``url`` to a new timestamped file in ``out_dir``. Never overwrites."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"aurelius-{now.strftime('%Y%m%dT%H%M%SZ')}.dump"
    if target.exists():
        raise BackupError(f"{target} already exists")
    partial = target.with_suffix(".partial")
    command = resolve("pg_dump", url, major)
    try:
        with partial.open("wb") as out:
            _run(command, ["--format=custom", "--no-owner", "--no-privileges"], stdout=out, stdin=subprocess.DEVNULL)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def restore(url: URL, major: int, dump: Path, *, force: bool, empty: bool) -> None:
    """Restore ``dump`` into ``url``. Refuses a non-empty database unless ``force``."""
    if not dump.is_file():
        raise BackupError(f"{dump} does not exist")
    if not empty and not force:
        raise BackupError(f"database {url.database!r} is not empty; restore refuses it without --force")
    command = resolve("pg_restore", url, major)
    extra = ["--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction"]
    if force:
        extra += ["--clean", "--if-exists"]
    with dump.open("rb") as source:
        _run(command, extra, stdin=source, stdout=subprocess.DEVNULL)
