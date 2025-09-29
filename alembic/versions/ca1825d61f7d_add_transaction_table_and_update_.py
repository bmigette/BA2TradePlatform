"""Add Transaction table and update relationships

Revision ID: ca1825d61f7d
Revises: c2379c5688f2
Create Date: 2025-09-29 10:11:27.368397

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ca1825d61f7d'
down_revision: Union[str, Sequence[str], None] = 'c2379c5688f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Transaction table already exists (created by SQLModel), so we skip creating it
    # Just add the missing column to tradingorder table using batch mode for SQLite
    
    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        batch_op.add_column(sa.Column('transaction_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_tradingorder_transaction', 'transaction', ['transaction_id'], ['id'], ondelete='CASCADE')


def downgrade() -> None:
    """Downgrade schema."""
    # Remove foreign key and column from tradingorder using batch mode for SQLite
    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        batch_op.drop_constraint('fk_tradingorder_transaction', type_='foreignkey')
        batch_op.drop_column('transaction_id')
    
    # Note: We don't drop the transaction table since it was created by SQLModel
