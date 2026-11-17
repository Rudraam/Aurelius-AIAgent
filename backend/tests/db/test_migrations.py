"""Migrations: they build exactly the ORM schema, downgrade cleanly, and install the guards (invariant 5)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.migrate import downgrade, upgrade
from app.db.tables import APPEND_ONLY, PLAN_TRANSITIONS, Base
from tests.db.conftest import create_database, drop_database

NOW = datetime(2027, 1, 1, 12, tzinfo=UTC)
STATES = ("proposed", "approved", "revised", "voided")


async def _tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        return {r[0] for r in rows}


async def test_metadata_matches_the_migrated_schema(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        diff = await conn.run_sync(
            lambda sync: compare_metadata(MigrationContext.configure(sync, opts={"compare_type": True}), Base.metadata)
        )
    assert diff == []


async def test_downgrade_then_upgrade_round_trips(admin_url: URL) -> None:
    name = f"aurelius_m_{uuid.uuid4().hex[:10]}"
    url = await create_database(admin_url, name)
    engine = create_async_engine(url)
    try:
        await upgrade(engine)
        assert set(Base.metadata.tables) <= await _tables(engine)
        await downgrade(engine, "base")
        assert await _tables(engine) == {"alembic_version"}
        async with engine.connect() as conn:
            functions = await conn.execute(text("SELECT proname FROM pg_proc WHERE proname LIKE 'aurelius_%'"))
            assert functions.all() == []
        await upgrade(engine)
        assert set(Base.metadata.tables) <= await _tables(engine)
    finally:
        await engine.dispose()
        await drop_database(admin_url, name)


async def _seed_plan(engine: AsyncEngine, state: str = "proposed") -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO income_source VALUES ('pay', 'Pay', 'biweekly', '2027-01-01', 15, 31, NULL, true,"
                " :now, :now, NULL)"
            ),
            {"now": NOW},
        )
        await conn.execute(text("INSERT INTO allocation_policy VALUES (1, '{}', :now)"), {"now": NOW})
        await conn.execute(
            text("INSERT INTO paycheque (income_source_id, payday, net_pay, created_at) VALUES ('pay', :d, 100, :now)"),
            {"d": date(2027, 1, 1), "now": NOW},
        )
        await conn.execute(
            text(
                "INSERT INTO allocation_plan (paycheque_id, state, status, policy_version, inputs_hash, snapshot,"
                " plan_json, payday, net_pay, created_at) SELECT id, :state, 'ok', 1, :h, '\\x00', '\\x00', payday,"
                " net_pay, :now FROM paycheque"
            ),
            {"state": state, "h": "0" * 64, "now": NOW},
        )
        await conn.execute(
            text(
                "INSERT INTO plan_event (plan_id, kind, at, detail) SELECT id, 'proposed', :now, '{}'"
                " FROM allocation_plan"
            ),
            {"now": NOW},
        )


async def _fails(engine: AsyncEngine, sql: str) -> str:
    with pytest.raises(DBAPIError) as caught:
        async with engine.begin() as conn:
            await conn.execute(text(sql))
    return str(caught.value)


@pytest.mark.parametrize(
    ("table", "column"), [("paycheque", "net_pay"), ("plan_event", "kind"), ("allocation_policy", "document")]
)
async def test_history_is_append_only(engine: AsyncEngine, table: str, column: str) -> None:
    await _seed_plan(engine)
    assert "append-only" in await _fails(engine, f"UPDATE {table} SET {column} = {column}")
    assert "append-only" in await _fails(engine, f"DELETE FROM {table}")
    assert "append-only" in await _fails(engine, f"TRUNCATE {table} CASCADE")


async def test_every_append_only_table_has_both_triggers(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT tgrelid::regclass::text, tgname FROM pg_trigger WHERE NOT tgisinternal"))
        triggers = {(r[0], r[1]) for r in rows}
    for table in APPEND_ONLY:
        assert (table, f"{table}_append_only") in triggers
        assert (table, f"{table}_no_truncate") in triggers
    assert ("allocation_plan", "allocation_plan_guard") in triggers
    assert ("allocation_plan", "allocation_plan_no_truncate") in triggers


async def test_a_stored_plan_is_immutable_except_its_state(engine: AsyncEngine) -> None:
    await _seed_plan(engine, state="approved")
    assert "immutable" in await _fails(engine, "UPDATE allocation_plan SET net_pay = 1")
    assert "immutable" in await _fails(engine, "UPDATE allocation_plan SET state = 'voided', net_pay = 1")
    assert "not allowed" in await _fails(engine, "DELETE FROM allocation_plan")
    assert "not allowed" in await _fails(engine, "TRUNCATE allocation_plan CASCADE")


@pytest.mark.parametrize(
    ("start", "end"),
    [(a, b) for a in STATES for b in STATES if a != b],
)
async def test_plan_state_transitions(engine: AsyncEngine, start: str, end: str) -> None:
    await _seed_plan(engine, state=start)
    sql = f"UPDATE allocation_plan SET state = '{end}'"
    if (start, end) in PLAN_TRANSITIONS:
        async with engine.begin() as conn:
            await conn.execute(text(sql))
    else:
        assert "is not allowed" in await _fails(engine, sql)


async def test_one_live_plan_per_paycheque(engine: AsyncEngine) -> None:
    await _seed_plan(engine)
    message = await _fails(
        engine,
        "INSERT INTO allocation_plan (paycheque_id, state, status, policy_version, inputs_hash, snapshot, plan_json,"
        " payday, net_pay, created_at) SELECT paycheque_id, 'approved', 'ok', 1, inputs_hash, snapshot, plan_json,"
        " payday, net_pay, created_at FROM allocation_plan",
    )
    assert "uq_allocation_plan_live_per_paycheque" in message


async def test_at_most_one_active_income_source(engine: AsyncEngine) -> None:
    await _seed_plan(engine)
    message = await _fails(
        engine,
        "INSERT INTO income_source VALUES ('side', 'Side', 'monthly', '2027-01-01', 15, 31, NULL, true, now(), now(),"
        " NULL)",
    )
    assert "uq_income_source_one_active" in message


async def test_money_columns_are_numeric_18_2(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name, column_name, numeric_precision, numeric_scale FROM information_schema.columns"
                " WHERE table_schema = 'public' AND data_type = 'numeric'"
            )
        )
        found = rows.all()
    assert found
    assert all((r[2], r[3]) == (18, 2) for r in found), found
