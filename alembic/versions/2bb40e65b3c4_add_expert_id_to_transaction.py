"""add_expert_id_to_transaction

Revision ID: 2bb40e65b3c4
Revises: aba75a452694
Create Date: 2025-09-29 11:38:39.213851

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2bb40e65b3c4'
down_revision: Union[str, Sequence[str], None] = 'aba75a452694'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add expert_id column to transaction table
    # SQLite doesn't support adding foreign key constraints to existing tables
    # so we'll just add the column
    op.add_column('transaction', sa.Column('expert_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Drop expert_id column
    op.drop_column('transaction', 'expert_id')
