"""Merge migration heads

Revision ID: fe4892ad6036
Revises: 90c713a1c75b, a92483497fab
Create Date: 2025-09-24 15:42:31.329169

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fe4892ad6036'
down_revision: Union[str, Sequence[str], None] = ('90c713a1c75b', 'a92483497fab')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
