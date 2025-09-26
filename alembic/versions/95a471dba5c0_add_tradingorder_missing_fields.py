"""add_tradingorder_missing_fields

Revision ID: 95a471dba5c0
Revises: c1a3b637d1b9
Create Date: 2025-09-26 12:00:34.635732

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '95a471dba5c0'
down_revision: Union[str, Sequence[str], None] = 'c1a3b637d1b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add missing columns to tradingorder table
    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        # Add comment field
        batch_op.add_column(sa.Column('comment', sa.String(), nullable=True))
        
        # Add order_recommendation_id field with foreign key
        batch_op.add_column(sa.Column('order_recommendation_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_tradingorder_order_recommendation_id',
            'expertrecommendation',
            ['order_recommendation_id'],
            ['id'],
            ondelete='SET NULL'
        )
        
        # Add open_type field with default value
        batch_op.add_column(sa.Column('open_type', sa.String(), nullable=False, server_default='MANUAL'))


def downgrade() -> None:
    """Downgrade schema."""
    # Remove added columns from tradingorder table
    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        batch_op.drop_constraint('fk_tradingorder_order_recommendation_id', type_='foreignkey')
        batch_op.drop_column('open_type')
        batch_op.drop_column('order_recommendation_id')
        batch_op.drop_column('comment')
