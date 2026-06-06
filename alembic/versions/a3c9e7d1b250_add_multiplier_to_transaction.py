"""add multiplier to transaction

Revision ID: a3c9e7d1b250
Revises: c7e1a4f90b2d
Create Date: 2026-06-06

Adds a nullable contract-multiplier column to Transaction so option P&L / value
math scales the per-share premium correctly (100 for standard options). Equity
rows leave it null and are treated as multiplier 1, so existing P&L is unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3c9e7d1b250'
down_revision: Union[str, None] = 'c7e1a4f90b2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.add_column("transaction", sa.Column("multiplier", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("transaction", "multiplier")
