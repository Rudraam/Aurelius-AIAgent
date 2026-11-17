"""Alembic environment.

Two ways in:
- ``app.db.migrate`` passes an open synchronous connection in ``config.attributes["connection"]``
  (from ``AsyncConnection.run_sync``), so migrations run inside the caller's event loop;
- the ``alembic`` CLI (from ``backend/``) gets none, and this file opens an async engine on
  ``AURELIUS_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from app.db.session import database_url, make_engine
from app.db.tables import Base

target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = make_engine(database_url())
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_run)
            await conn.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline (--sql) migrations are not supported")

_connection = context.config.attributes.get("connection")
if _connection is not None:
    _run(_connection)
else:
    asyncio.run(_run_async())
