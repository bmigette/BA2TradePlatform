"""remove_price_fields_from_trading_order

Revision ID: db671057a6b9
Revises: 95a471dba5c0
Create Date: 2025-09-26 12:29:12.467230

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'db671057a6b9'
down_revision: Union[str, Sequence[str], None] = '95a471dba5c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Only remove the price fields from tradingorder table (SQLite compatible)
    op.drop_column('tradingorder', 'client_order_id')
    op.drop_column('tradingorder', 'limit_price')
    op.drop_column('tradingorder', 'stop_price')


def downgrade() -> None:
    """Downgrade schema."""
    # Add back the price fields to tradingorder table
    op.add_column('tradingorder', sa.Column('stop_price', sa.FLOAT(), nullable=True))
    op.add_column('tradingorder', sa.Column('limit_price', sa.FLOAT(), nullable=True))
    op.add_column('tradingorder', sa.Column('client_order_id', sa.VARCHAR(), nullable=True))
