"""remove_filled_avg_price_from_tradingorder

Revision ID: 39406a9ffef1
Revises: 648ce01dcd39
Create Date: 2025-10-07 21:30:04.248033

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '39406a9ffef1'
down_revision: Union[str, Sequence[str], None] = '648ce01dcd39'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - remove filled_avg_price column from tradingorder table."""
    # Remove filled_avg_price column from tradingorder table
    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        batch_op.drop_column('filled_avg_price')


def downgrade() -> None:
    """Downgrade schema - add back filled_avg_price column to tradingorder table."""
    # Add back filled_avg_price column to tradingorder table
    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        batch_op.add_column(sa.Column('filled_avg_price', sa.Float(), nullable=True))
