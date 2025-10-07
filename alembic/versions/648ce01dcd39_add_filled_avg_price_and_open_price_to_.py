"""add_filled_avg_price_and_open_price_to_tradingorder

Revision ID: 648ce01dcd39
Revises: 0d97964e8ad8
Create Date: 2025-10-07 10:25:14.325386

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '648ce01dcd39'
down_revision: Union[str, Sequence[str], None] = '0d97964e8ad8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add filled_avg_price and open_price columns to tradingorder table
    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        batch_op.add_column(sa.Column('filled_avg_price', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('open_price', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Remove filled_avg_price and open_price columns from tradingorder table
    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        batch_op.drop_column('open_price')
        batch_op.drop_column('filled_avg_price')
