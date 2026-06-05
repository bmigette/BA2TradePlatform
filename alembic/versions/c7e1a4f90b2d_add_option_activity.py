"""add option activity

Revision ID: c7e1a4f90b2d
Revises: 5b8b9ed9f0f4
Create Date: 2026-06-05

Creates the option_activity table: an audit + idempotency record for processed
broker option lifecycle events (assignment / exercise / expiry / cash-settle).
The (account_id, activity_id) pair is the idempotency key for reconciliation so
each broker activity is applied exactly once.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7e1a4f90b2d'
down_revision: Union[str, None] = '5b8b9ed9f0f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.create_table(
        "option_activity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("activity_id", sa.String(), nullable=False),
        sa.Column("activity_type", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("qty", sa.Float(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
        sa.Column("result", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_option_activity_account_id", "option_activity", ["account_id"])
    op.create_index("ix_option_activity_activity_id", "option_activity", ["activity_id"])
    op.create_index("ix_option_activity_processed_at", "option_activity", ["processed_at"])


def downgrade():
    op.drop_index("ix_option_activity_processed_at", table_name="option_activity")
    op.drop_index("ix_option_activity_activity_id", table_name="option_activity")
    op.drop_index("ix_option_activity_account_id", table_name="option_activity")
    op.drop_table("option_activity")
