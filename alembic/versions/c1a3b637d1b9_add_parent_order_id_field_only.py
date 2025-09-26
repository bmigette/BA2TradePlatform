"""add_parent_order_id_field_only

Revision ID: c1a3b637d1b9
Revises: 9c81daba71ae
Create Date: 2025-09-26 11:37:45.197314

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1a3b637d1b9'
down_revision: Union[str, Sequence[str], None] = '3955ee899517'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Use batch mode for SQLite compatibility
    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_order_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_tradingorder_parent_order_id',
            'tradingorder',
            ['parent_order_id'],
            ['id'],
            ondelete='CASCADE'
        )


def downgrade() -> None:
    """Downgrade schema."""
    # Use batch mode for SQLite compatibility
    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        batch_op.drop_constraint('fk_tradingorder_parent_order_id', type_='foreignkey')
        batch_op.drop_column('parent_order_id')
