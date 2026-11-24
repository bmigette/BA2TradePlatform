"""add the account_growth_snapshot and symbol_dividend_history tables (stored page snapshots)

Revision ID: f4c2a8d61b39
Revises: e3b7c5a91f20
Create Date: 2026-11-24

The Account Growth tab paints from ``account_growth_snapshot`` (dataset "growth") and the allocator page reads its
dividend rows from it (dataset "dividends"); the dividend forecast keeps FMP's raw dividend histories in
``symbol_dividend_history`` across restarts. Both start empty: nothing needs a default and no existing row is touched.
IDEMPOTENT, like e3b7c5a91f20: ``main.initialize_system`` imports the in-tree models BEFORE ``init_db()``, so
``create_all`` may already have built the tables; every create is guarded by ``has_table``. Downgrade drops both
(the stored copies are lost; the pages then wait for the broker / FMP again, nothing else changes).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f4c2a8d61b39'
down_revision: Union[str, Sequence[str], None] = 'e3b7c5a91f20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SNAPSHOT = 'account_growth_snapshot'
HISTORY = 'symbol_dividend_history'


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(SNAPSHOT):
        op.create_table(
            SNAPSHOT,
            sa.Column('account_id', sa.Integer(),
                      sa.ForeignKey('accountdefinition.id', ondelete='CASCADE'), nullable=False),
            sa.Column('dataset', sa.String(), nullable=False),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            sa.Column('payload', sa.JSON(), nullable=False),
            sa.Column('schema_version', sa.Integer(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('account_id', 'dataset'),
        )
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(HISTORY):
        op.create_table(
            HISTORY,
            sa.Column('symbol', sa.String(), nullable=False),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            sa.Column('payload', sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint('symbol'),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in (HISTORY, SNAPSHOT):
        if inspector.has_table(table):
            op.drop_table(table)
