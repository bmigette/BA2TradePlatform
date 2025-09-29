"""Rename transaction_recommendation_id to expert_recommendation_id

Revision ID: aba75a452694
Revises: ca1825d61f7d
Create Date: 2025-09-29 11:06:50.459182

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'aba75a452694'
down_revision: Union[str, Sequence[str], None] = 'ca1825d61f7d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename transaction_recommendation_id to expert_recommendation_id in transaction table."""
    # For SQLite, we need to recreate the table since renaming columns with foreign keys is complex
    # First, let's rename the old column using ALTER TABLE
    op.execute('ALTER TABLE [transaction] RENAME COLUMN transaction_recommendation_id TO expert_recommendation_id')
    
    # Update the foreign key reference if needed (SQLite might not enforce this immediately)
    # The foreign key will be properly set up when the models are next initialized


def downgrade() -> None:
    """Rename expert_recommendation_id back to transaction_recommendation_id."""
    op.execute('ALTER TABLE [transaction] RENAME COLUMN expert_recommendation_id TO transaction_recommendation_id')
