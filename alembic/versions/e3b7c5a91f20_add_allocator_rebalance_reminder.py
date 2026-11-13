"""add the allocator_rebalance_reminder table (per-account interval of the "rebalance due" reminder)

Revision ID: e3b7c5a91f20
Revises: d5a1f93e2c70
Create Date: 2026-11-13

One row per account that changed the interval; absence == the documented default (3 months, in
``core.allocator_rebalance_reminder``), so nothing needs a default and no existing row is touched.
IDEMPOTENT, like d5a1f93e2c70: ``main.initialize_system`` imports the in-tree models BEFORE ``init_db()``,
so ``create_all`` may already have built the table; every create is guarded by ``has_table`` /
``has_index``. Downgrade drops the table (the chosen intervals are lost; nothing at the broker changes).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e3b7c5a91f20'
down_revision: Union[str, Sequence[str], None] = 'd5a1f93e2c70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'allocator_rebalance_reminder'
INDEX = 'ix_allocator_rebalance_reminder_account_id'


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('account_id', sa.Integer(),
                      sa.ForeignKey('accountdefinition.id', ondelete='CASCADE'), nullable=False),
            sa.Column('interval_months', sa.Integer(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_index(TABLE, INDEX):
        op.create_index(INDEX, TABLE, ['account_id'], unique=True)


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
