"""merge_provider_and_trade_action_migrations

Revision ID: fae5a5e0e1fb
Revises: 73484cedee2e, bdcb4237ecdc
Create Date: 2025-10-08 18:02:42.380704

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fae5a5e0e1fb'
down_revision: Union[str, Sequence[str], None] = ('73484cedee2e', 'bdcb4237ecdc')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
