"""Add order dependency fields to TradingOrder table

Revision ID: 0ed865bdeda6
Revises: 7bf9a0deb7bf
Create Date: 2025-09-29 23:03:41.213515

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0ed865bdeda6'
down_revision: Union[str, Sequence[str], None] = '7bf9a0deb7bf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
