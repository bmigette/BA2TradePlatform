"""rename_virtual_equity_and_add_account_order_id

Revision ID: 7bf9a0deb7bf
Revises: 2bb40e65b3c4
Create Date: 2025-09-29 11:58:59.484492

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7bf9a0deb7bf'
down_revision: Union[str, Sequence[str], None] = '2bb40e65b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add account_order_id column to TradingOrder table
    op.add_column('tradingorder', sa.Column('account_order_id', sa.String(), nullable=True))
    
    # Rename virtual_equity to virtual_equity_pct in ExpertInstance table
    with op.batch_alter_table('expertinstance') as batch_op:
        batch_op.alter_column('virtual_equity', new_column_name='virtual_equity_pct')


def downgrade() -> None:
    """Downgrade schema."""
    # Remove account_order_id column from TradingOrder table
    op.drop_column('tradingorder', 'account_order_id')
    
    # Rename virtual_equity_pct back to virtual_equity in ExpertInstance table
    with op.batch_alter_table('expertinstance') as batch_op:
        batch_op.alter_column('virtual_equity_pct', new_column_name='virtual_equity')
