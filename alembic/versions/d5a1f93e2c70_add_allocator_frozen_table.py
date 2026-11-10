"""add the allocator_frozen table (per-symbol freeze of the portfolio allocator)

Revision ID: d5a1f93e2c70
Revises: c4e7a9d2f1b8
Create Date: 2026-11-10

One row per (account, symbol) the operator froze; absence == not frozen, so nothing needs a default
and no existing row is touched. IDEMPOTENT, like a7c3e91d5b24: ``main.initialize_system`` imports the
in-tree models BEFORE ``init_db()``, so ``create_all`` may already have built the table; every create is
guarded by ``has_table`` / ``has_index``. Downgrade drops the table (the freezes are lost; nothing at the
broker changes).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5a1f93e2c70'
down_revision: Union[str, Sequence[str], None] = 'c4e7a9d2f1b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table('allocator_frozen'):
        op.create_table(
            'allocator_frozen',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('account_id', sa.Integer(),
                      sa.ForeignKey('accountdefinition.id', ondelete='CASCADE'), nullable=False),
            sa.Column('symbol', sa.String(), nullable=False),
            sa.Column('since', sa.DateTime(), nullable=False),
            sa.Column('note', sa.String(), nullable=True),
            sa.UniqueConstraint('account_id', 'symbol', name='uix_alloc_frozen_account_symbol'),
        )
    inspector = sa.inspect(op.get_bind())
    for name, column in (('ix_allocator_frozen_account_id', 'account_id'),
                         ('ix_allocator_frozen_symbol', 'symbol')):
        if not inspector.has_index('allocator_frozen', name):
            op.create_index(name, 'allocator_frozen', [column])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('allocator_frozen'):
        op.drop_table('allocator_frozen')
