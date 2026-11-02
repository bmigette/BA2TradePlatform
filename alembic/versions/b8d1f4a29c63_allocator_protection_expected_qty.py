"""add allocator_protection.expected_qty

Revision ID: b8d1f4a29c63
Revises: a7c3e91d5b24
Create Date: 2026-10-05

The position a platform sale is expected to leave behind: the protection re-placement that follows
the sale waits until the broker's position read agrees (or the settle window passes), so a lagging
read cannot place protection on a position the sale just closed. Nullable, no backfill; the column is
added only when the table exists and lacks it (idempotent, same guards as a7c3e91d5b24).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8d1f4a29c63'
down_revision: Union[str, Sequence[str], None] = 'a7c3e91d5b24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table('allocator_protection'):
        return
    if 'expected_qty' in {c['name'] for c in inspector.get_columns('allocator_protection')}:
        print("[alloc-protection] allocator_protection.expected_qty already exists -- skipped")
        return
    op.add_column('allocator_protection', sa.Column('expected_qty', sa.Float(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table('allocator_protection') and \
            'expected_qty' in {c['name'] for c in inspector.get_columns('allocator_protection')}:
        op.drop_column('allocator_protection', 'expected_qty')
