"""add_trader_name_to_expert_recommendation

Revision ID: 71c04ab92529
Revises: 7195b1928379
Create Date: 2025-10-13 18:06:14.899335

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '71c04ab92529'
down_revision: Union[str, Sequence[str], None] = '7195b1928379'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add trader_name column to expertrecommendation table
    op.add_column('expertrecommendation', sa.Column('trader_name', sa.String(length=200), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Remove trader_name column from expertrecommendation table
    op.drop_column('expertrecommendation', 'trader_name')
