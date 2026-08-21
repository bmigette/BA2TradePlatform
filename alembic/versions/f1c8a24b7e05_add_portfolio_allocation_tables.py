"""add the five portfolio allocation tables

Revision ID: f1c8a24b7e05
Revises: f1a7c2e9b4d0
Create Date: 2026-08-20

Creates portfolio_allocation_config, portfolio_allocation_label,
portfolio_allocation_symbol, portfolio_income_event and portfolio_allocation_run.
Chained AFTER the instrument merge + unique index so the destructive data
migration can be run and inspected on its own, without the schema additions
riding along.

Index names are the ones SQLAlchemy itself emits for these models
(``ix_<table>_<column>``), so init_db()'s create_all on a fresh DB and Alembic on
an existing one agree. Foreign keys are declarative only -- the live DB runs with
PRAGMA foreign_keys = 0 -- so account deletion must clear these tables
explicitly (see portfolio_allocation_store.delete_account_allocation_data).

Amended before ever being applied (the live DB is still two revisions behind, at
d5e1b9a3c842, and no row of these tables exists anywhere) to add
portfolio_allocation_run.income_consumed_at / income_consumed_events: the
per-run idempotency guard for income consumption. Amending in place is only
legal because nothing has run this revision yet; once it ships, the same change
costs a second revision.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1c8a24b7e05'
down_revision: Union[str, Sequence[str], None] = 'f1a7c2e9b4d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "portfolio_allocation_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("valuation_mode", sa.String(), nullable=False),
        sa.Column("allow_fractional", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # unique=True + index=True on the model emits ONE unique index, not a
    # UniqueConstraint -- mirror that exactly so create_all and Alembic agree.
    op.create_index("ix_portfolio_allocation_config_account_id",
                    "portfolio_allocation_config", ["account_id"], unique=True)
    op.create_index("ix_portfolio_allocation_config_updated_at",
                    "portfolio_allocation_config", ["updated_at"])

    op.create_table(
        "portfolio_allocation_label",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("target_pct", sa.Float(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "label", name="uix_pf_alloc_label_account_label"),
    )
    op.create_index("ix_portfolio_allocation_label_account_id", "portfolio_allocation_label", ["account_id"])
    op.create_index("ix_portfolio_allocation_label_label", "portfolio_allocation_label", ["label"])
    op.create_index("ix_portfolio_allocation_label_created_at", "portfolio_allocation_label", ["created_at"])

    op.create_table(
        "portfolio_allocation_symbol",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("weight_pct", sa.Float(), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "label", "symbol",
                            name="uix_pf_alloc_symbol_account_label_symbol"),
    )
    op.create_index("ix_portfolio_allocation_symbol_account_id", "portfolio_allocation_symbol", ["account_id"])
    op.create_index("ix_portfolio_allocation_symbol_label", "portfolio_allocation_symbol", ["label"])
    op.create_index("ix_portfolio_allocation_symbol_symbol", "portfolio_allocation_symbol", ["symbol"])
    op.create_index("ix_portfolio_allocation_symbol_created_at", "portfolio_allocation_symbol", ["created_at"])

    op.create_table(
        "portfolio_income_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("consumed_amount", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "external_id", name="uix_pf_income_account_externalid"),
    )
    op.create_index("ix_portfolio_income_event_account_id", "portfolio_income_event", ["account_id"])
    op.create_index("ix_portfolio_income_event_external_id", "portfolio_income_event", ["external_id"])
    op.create_index("ix_portfolio_income_event_event_date", "portfolio_income_event", ["event_date"])
    op.create_index("ix_portfolio_income_event_symbol", "portfolio_income_event", ["symbol"])
    op.create_index("ix_portfolio_income_event_created_at", "portfolio_income_event", ["created_at"])

    op.create_table(
        "portfolio_allocation_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("scope_label", sa.String(), nullable=True),
        sa.Column("base_notional", sa.Float(), nullable=False),
        sa.Column("available_buying_power", sa.Float(), nullable=False),
        sa.Column("allow_fractional", sa.Boolean(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("submitted_buy_value", sa.Float(), nullable=False),
        sa.Column("submitted_sell_value", sa.Float(), nullable=False),
        sa.Column("order_ids", sa.JSON(), nullable=True),
        # Income-ledger replay guard. NULL = this run has never consumed income;
        # a timestamp = it has, exactly once. Written in the SAME transaction as
        # the portfolio_income_event updates (see finalise_allocation_run), so
        # the check and the spend cannot interleave.
        sa.Column("income_consumed_at", sa.DateTime(), nullable=True),
        sa.Column("income_consumed_events", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_portfolio_allocation_run_account_id", "portfolio_allocation_run", ["account_id"])
    op.create_index("ix_portfolio_allocation_run_mode", "portfolio_allocation_run", ["mode"])
    op.create_index("ix_portfolio_allocation_run_created_at", "portfolio_allocation_run", ["created_at"])


def downgrade() -> None:
    op.drop_table("portfolio_allocation_run")
    op.drop_table("portfolio_income_event")
    op.drop_table("portfolio_allocation_symbol")
    op.drop_table("portfolio_allocation_label")
    op.drop_table("portfolio_allocation_config")
