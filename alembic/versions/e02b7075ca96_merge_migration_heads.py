"""merge_migration_heads

Revision ID: e02b7075ca96
Revises: 71c04ab92529, b7c3d9f5a1e8
Create Date: 2025-10-22 11:47:18.112585

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e02b7075ca96'
down_revision: Union[str, Sequence[str], None] = ('71c04ab92529', 'b7c3d9f5a1e8')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
