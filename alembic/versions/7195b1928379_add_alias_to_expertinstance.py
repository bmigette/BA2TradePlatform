"""add_alias_to_expertinstance

Revision ID: 7195b1928379
Revises: 9499764147b9
Create Date: 2025-10-09 21:47:37.077379

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7195b1928379'
down_revision: Union[str, Sequence[str], None] = '9499764147b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add alias column to expertinstance table
    op.add_column('expertinstance', sa.Column('alias', sa.String(length=100), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Remove alias column from expertinstance table
    op.drop_column('expertinstance', 'alias')
