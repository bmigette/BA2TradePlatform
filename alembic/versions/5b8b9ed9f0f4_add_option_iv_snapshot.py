"""add option iv snapshot

Revision ID: 5b8b9ed9f0f4
Revises: 08de6c7b6eed
Create Date: 2026-06-05

Creates the option_iv_snapshot table to persist a trailing ATM implied-volatility
series per (account, underlying). Brokers such as Alpaca expose no IV history, so
we store our own samples and compute IV-rank as a percentile over the window.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5b8b9ed9f0f4'
down_revision: Union[str, None] = '08de6c7b6eed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.create_table(
        "option_iv_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("underlying", sa.String(), nullable=False),
        sa.Column("atm_iv", sa.Float(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_option_iv_snapshot_account_id", "option_iv_snapshot", ["account_id"])
    op.create_index("ix_option_iv_snapshot_underlying", "option_iv_snapshot", ["underlying"])
    op.create_index("ix_option_iv_snapshot_recorded_at", "option_iv_snapshot", ["recorded_at"])


def downgrade():
    op.drop_index("ix_option_iv_snapshot_recorded_at", table_name="option_iv_snapshot")
    op.drop_index("ix_option_iv_snapshot_underlying", table_name="option_iv_snapshot")
    op.drop_index("ix_option_iv_snapshot_account_id", table_name="option_iv_snapshot")
    op.drop_table("option_iv_snapshot")
