"""Run Alembic migrations from code, inside the caller's event loop."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
HEAD = "head"


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


async def upgrade(engine: AsyncEngine, revision: str = HEAD) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: command.upgrade(alembic_config(sync), revision))


async def downgrade(engine: AsyncEngine, revision: str) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: command.downgrade(alembic_config(sync), revision))
