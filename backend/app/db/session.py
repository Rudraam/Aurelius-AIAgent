"""Engine and sessions. The database URL comes from ``AURELIUS_DATABASE_URL`` and has no default."""

from __future__ import annotations

import os

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL_ENV = "AURELIUS_DATABASE_URL"
DRIVER = "postgresql+asyncpg"


class DatabaseConfigError(RuntimeError):
    pass


def parse_url(raw: str) -> URL:
    """A ``postgresql+asyncpg://`` URL. A bare ``postgresql://`` gets the asyncpg driver."""
    url = make_url(raw)
    if url.drivername == "postgresql":
        url = url.set(drivername=DRIVER)
    if url.drivername != DRIVER:
        raise DatabaseConfigError(f"{DATABASE_URL_ENV} must be a postgresql:// URL, not {url.drivername}://")
    if not url.database:
        raise DatabaseConfigError(f"{DATABASE_URL_ENV} must name a database")
    return url


def database_url() -> URL:
    raw = os.environ.get(DATABASE_URL_ENV)
    if not raw:
        raise DatabaseConfigError(
            f"{DATABASE_URL_ENV} is not set, e.g. postgresql://aurelius:<password>@127.0.0.1:5433/aurelius"
        )
    return parse_url(raw)


def make_engine(url: URL | str) -> AsyncEngine:
    return create_async_engine(parse_url(url) if isinstance(url, str) else url, pool_pre_ping=True)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
