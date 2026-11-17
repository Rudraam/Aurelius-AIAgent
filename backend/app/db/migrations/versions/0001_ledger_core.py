"""Ledger core: config, append-only history, plans and their guards.

Revision ID: 0001
Revises:
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(18, 2)
SLUG = r"^[a-z0-9][a-z0-9-]*$"

APPEND_ONLY = (
    "balance_entry",
    "balance_drift",
    "contribution_room",
    "room_movement",
    "goal_adjustment",
    "allocation_policy",
    "paycheque",
    "plan_line",
    "plan_event",
)

APPEND_ONLY_FUNCTION = """
CREATE FUNCTION aurelius_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'aurelius: % on % is not allowed; the table is append-only', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END
$$;
"""

PLAN_GUARD_FUNCTION = """
CREATE FUNCTION aurelius_plan_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'aurelius: % on allocation_plan is not allowed', TG_OP
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (to_jsonb(NEW) - 'state') IS DISTINCT FROM (to_jsonb(OLD) - 'state') THEN
        RAISE EXCEPTION 'aurelius: a stored plan is immutable; only its state may change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NOT ((OLD.state, NEW.state) IN (
        ('proposed', 'approved'), ('proposed', 'revised'), ('proposed', 'voided'), ('approved', 'voided')
    )) THEN
        RAISE EXCEPTION 'aurelius: plan state % -> % is not allowed', OLD.state, NEW.state
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;
"""


def _ts(name: str, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _config_columns() -> list[sa.Column[Any]]:
    return [_ts("created_at"), _ts("updated_at"), _ts("seeded_at", nullable=True)]


def upgrade() -> None:
    op.create_table(
        "income_source",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("frequency", sa.String(16), nullable=False),
        sa.Column("anchor", sa.Date(), nullable=False),
        sa.Column("semimonthly_first", sa.Integer(), nullable=False),
        sa.Column("semimonthly_second", sa.Integer(), nullable=False),
        sa.Column("expected_net", MONEY, nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_config_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_income_source"),
        sa.CheckConstraint(f"id ~ '{SLUG}'", name="ck_income_source_slug"),
        sa.CheckConstraint(
            "frequency IN ('weekly', 'biweekly', 'semimonthly', 'monthly')", name="ck_income_source_frequency"
        ),
        sa.CheckConstraint(
            "semimonthly_first BETWEEN 1 AND 28 AND semimonthly_second > semimonthly_first"
            " AND semimonthly_second <= 31",
            name="ck_income_source_semimonthly_days",
        ),
        sa.CheckConstraint("expected_net IS NULL OR expected_net >= 0", name="ck_income_source_expected_net"),
    )
    op.create_index(
        "uq_income_source_one_active",
        "income_source",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    op.create_table(
        "account",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("institution", sa.Text(), nullable=True),
        sa.Column("roles", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_config_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_account"),
        sa.CheckConstraint(f"id ~ '{SLUG}'", name="ck_account_slug"),
        sa.CheckConstraint(
            "kind IN ('chequing', 'hisa', 'gic', 'tfsa', 'fhsa', 'rrsp', 'non_registered')", name="ck_account_kind"
        ),
        sa.CheckConstraint("currency IN ('CAD', 'USD')", name="ck_account_currency"),
    )

    op.create_table(
        "bill",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("frequency", sa.String(16), nullable=False),
        sa.Column("anchor", sa.Date(), nullable=False),
        sa.Column("day_of_month", sa.Integer(), nullable=True),
        sa.Column("interval_months", sa.Integer(), nullable=True),
        sa.Column("autopay", sa.Boolean(), nullable=False),
        sa.Column("critical", sa.Boolean(), nullable=False),
        sa.Column("essential", sa.Boolean(), nullable=False),
        sa.Column("sinking", sa.Boolean(), nullable=False),
        sa.Column("pay_from_account_id", sa.String(64), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_config_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_bill"),
        sa.ForeignKeyConstraint(["pay_from_account_id"], ["account.id"], name="fk_bill_pay_from_account_id_account"),
        sa.CheckConstraint(f"id ~ '{SLUG}'", name="ck_bill_slug"),
        sa.CheckConstraint("amount > 0", name="ck_bill_amount"),
        sa.CheckConstraint(
            "frequency IN ('monthly', 'biweekly', 'annual', 'every_n_months')", name="ck_bill_frequency"
        ),
        sa.CheckConstraint("day_of_month IS NULL OR day_of_month BETWEEN 1 AND 31", name="ck_bill_day_of_month"),
        sa.CheckConstraint("interval_months IS NULL OR interval_months >= 1", name="ck_bill_interval_months"),
    )

    op.create_table(
        "budget_line",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_config_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_budget_line"),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], name="fk_budget_line_account_id_account"),
        sa.CheckConstraint(f"id ~ '{SLUG}'", name="ck_budget_line_slug"),
        sa.CheckConstraint("amount >= 0", name="ck_budget_line_amount"),
        sa.CheckConstraint("period IN ('per_pay', 'per_month')", name="ck_budget_line_period"),
    )

    op.create_table(
        "goal",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("target", MONEY, nullable=False),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("saved_before_aurelius", MONEY, nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_config_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_goal"),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], name="fk_goal_account_id_account"),
        sa.CheckConstraint(f"id ~ '{SLUG}'", name="ck_goal_slug"),
        sa.CheckConstraint("target > 0", name="ck_goal_target"),
        sa.CheckConstraint("saved_before_aurelius >= 0", name="ck_goal_saved_before_aurelius"),
    )

    op.create_table(
        "bank_holiday",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        *_config_columns(),
        sa.PrimaryKeyConstraint("day", name="pk_bank_holiday"),
    )

    op.create_table(
        "balance_entry",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_balance_entry"),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], name="fk_balance_entry_account_id_account"),
        sa.UniqueConstraint("account_id", "as_of", name="uq_balance_entry_account_id_as_of"),
        sa.CheckConstraint("source IN ('manual', 'questrade_sync')", name="ck_balance_entry_source"),
    )

    op.create_table(
        "balance_drift",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("balance_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("projected", MONEY, nullable=False),
        sa.Column("actual", MONEY, nullable=False),
        sa.Column("difference", MONEY, nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_balance_drift"),
        sa.ForeignKeyConstraint(
            ["balance_entry_id"], ["balance_entry.id"], name="fk_balance_drift_balance_entry_id_balance_entry"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], name="fk_balance_drift_account_id_account"),
        sa.UniqueConstraint("balance_entry_id", name="uq_balance_drift_balance_entry_id"),
        sa.CheckConstraint("difference = actual - projected", name="ck_balance_drift_difference"),
    )

    op.create_table(
        "contribution_room",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("room", MONEY, nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("source_note", sa.Text(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_contribution_room"),
        sa.UniqueConstraint("kind", "year", "as_of", name="uq_contribution_room_kind_year_as_of"),
        sa.CheckConstraint("kind IN ('tfsa', 'fhsa', 'rrsp')", name="ck_contribution_room_kind"),
        sa.CheckConstraint("length(source_note) > 0", name="ck_contribution_room_source_note"),
    )

    op.create_table(
        "room_movement",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("on", sa.Date(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("movement", sa.String(16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_room_movement"),
        sa.CheckConstraint("kind IN ('tfsa', 'fhsa', 'rrsp')", name="ck_room_movement_kind"),
        sa.CheckConstraint("amount > 0", name="ck_room_movement_amount"),
        sa.CheckConstraint("movement IN ('contribution', 'withdrawal')", name="ck_room_movement_movement"),
    )

    op.create_table(
        "goal_adjustment",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("goal_id", sa.String(64), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("on", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_goal_adjustment"),
        sa.ForeignKeyConstraint(["goal_id"], ["goal.id"], name="fk_goal_adjustment_goal_id_goal"),
        sa.CheckConstraint("amount <> 0", name="ck_goal_adjustment_amount"),
        sa.CheckConstraint("length(note) > 0", name="ck_goal_adjustment_note"),
    )

    op.create_table(
        "allocation_policy",
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("version", name="pk_allocation_policy"),
        sa.CheckConstraint("version >= 1", name="ck_allocation_policy_version"),
    )

    op.create_table(
        "paycheque",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("income_source_id", sa.String(64), nullable=False),
        sa.Column("payday", sa.Date(), nullable=False),
        sa.Column("net_pay", MONEY, nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_paycheque"),
        sa.ForeignKeyConstraint(
            ["income_source_id"], ["income_source.id"], name="fk_paycheque_income_source_id_income_source"
        ),
        sa.UniqueConstraint("payday", name="uq_paycheque_payday"),
        sa.CheckConstraint("net_pay >= 0", name="ck_paycheque_net_pay"),
    )

    op.create_table(
        "allocation_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("paycheque_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("inputs_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", sa.LargeBinary(), nullable=False),
        sa.Column("plan_json", sa.LargeBinary(), nullable=False),
        sa.Column("payday", sa.Date(), nullable=False),
        sa.Column("net_pay", MONEY, nullable=False),
        sa.Column("parent_plan_id", sa.BigInteger(), nullable=True),
        sa.Column("replaces_plan_id", sa.BigInteger(), nullable=True),
        sa.Column("overrides", postgresql.JSONB(), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_allocation_plan"),
        sa.ForeignKeyConstraint(["paycheque_id"], ["paycheque.id"], name="fk_allocation_plan_paycheque_id_paycheque"),
        sa.ForeignKeyConstraint(
            ["policy_version"],
            ["allocation_policy.version"],
            name="fk_allocation_plan_policy_version_allocation_policy",
        ),
        sa.ForeignKeyConstraint(
            ["parent_plan_id"], ["allocation_plan.id"], name="fk_allocation_plan_parent_plan_id_allocation_plan"
        ),
        sa.ForeignKeyConstraint(
            ["replaces_plan_id"], ["allocation_plan.id"], name="fk_allocation_plan_replaces_plan_id_allocation_plan"
        ),
        sa.CheckConstraint("state IN ('proposed', 'approved', 'revised', 'voided')", name="ck_allocation_plan_state"),
        sa.CheckConstraint("status IN ('ok', 'shortfall')", name="ck_allocation_plan_status"),
        sa.CheckConstraint("net_pay >= 0", name="ck_allocation_plan_net_pay"),
        sa.CheckConstraint("inputs_hash ~ '^[0-9a-f]{64}$'", name="ck_allocation_plan_inputs_hash"),
    )
    op.create_index("ix_allocation_plan_paycheque_id", "allocation_plan", ["paycheque_id"])
    op.create_index(
        "uq_allocation_plan_live_per_paycheque",
        "allocation_plan",
        ["paycheque_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('proposed', 'approved')"),
    )

    op.create_table(
        "plan_line",
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("line_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("bucket", sa.String(32), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=True),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("reason_params", postgresql.JSONB(), nullable=False),
        sa.Column("overridden_from", MONEY, nullable=True),
        sa.PrimaryKeyConstraint("plan_id", "line_id", name="pk_plan_line"),
        sa.ForeignKeyConstraint(["plan_id"], ["allocation_plan.id"], name="fk_plan_line_plan_id_allocation_plan"),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], name="fk_plan_line_account_id_account"),
        sa.CheckConstraint("amount >= 0", name="ck_plan_line_amount"),
    )

    op.create_table(
        "plan_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        _ts("at"),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_plan_event"),
        sa.ForeignKeyConstraint(["plan_id"], ["allocation_plan.id"], name="fk_plan_event_plan_id_allocation_plan"),
        sa.CheckConstraint("kind IN ('proposed', 'revised', 'approved', 'voided')", name="ck_plan_event_kind"),
    )
    op.create_index("ix_plan_event_plan_id", "plan_event", ["plan_id"])

    # Invariant 5: history is append-only, and a stored plan only moves along the allowed transitions.
    op.execute(APPEND_ONLY_FUNCTION)
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION aurelius_append_only()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}"
            " FOR EACH STATEMENT EXECUTE FUNCTION aurelius_append_only()"
        )
    op.execute(PLAN_GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER allocation_plan_guard BEFORE UPDATE OR DELETE ON allocation_plan"
        " FOR EACH ROW EXECUTE FUNCTION aurelius_plan_guard()"
    )
    op.execute(
        "CREATE TRIGGER allocation_plan_no_truncate BEFORE TRUNCATE ON allocation_plan"
        " FOR EACH STATEMENT EXECUTE FUNCTION aurelius_plan_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER allocation_plan_no_truncate ON allocation_plan")
    op.execute("DROP TRIGGER allocation_plan_guard ON allocation_plan")
    op.execute("DROP FUNCTION aurelius_plan_guard()")
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.execute("DROP FUNCTION aurelius_append_only()")
    op.drop_index("ix_plan_event_plan_id", table_name="plan_event")
    op.drop_table("plan_event")
    op.drop_table("plan_line")
    op.drop_index("uq_allocation_plan_live_per_paycheque", table_name="allocation_plan")
    op.drop_index("ix_allocation_plan_paycheque_id", table_name="allocation_plan")
    op.drop_table("allocation_plan")
    op.drop_table("paycheque")
    op.drop_table("allocation_policy")
    op.drop_table("goal_adjustment")
    op.drop_table("room_movement")
    op.drop_table("contribution_room")
    op.drop_table("balance_drift")
    op.drop_table("balance_entry")
    op.drop_table("bank_holiday")
    op.drop_table("goal")
    op.drop_table("budget_line")
    op.drop_table("bill")
    op.drop_table("account")
    op.drop_index("uq_income_source_one_active", table_name="income_source")
    op.drop_table("income_source")
