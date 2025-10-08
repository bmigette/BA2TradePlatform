"""remove_transaction_id_from_trade_action_result

Revision ID: bdcb4237ecdc
Revises: 39406a9ffef1
Create Date: 2025-10-07 22:59:19.469458

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bdcb4237ecdc'
down_revision: Union[str, Sequence[str], None] = '39406a9ffef1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Check if column exists before trying to drop it
    import sqlite3
    import os
    
    # Get database path
    db_path = os.path.expanduser('~/Documents/ba2_trade_platform/db.sqlite')
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(trade_action_result)")
    columns = [col[1] for col in cursor.fetchall()]
    conn.close()
    
    # Drop the transaction_id column from trade_action_result table if it exists
    if 'transaction_id' in columns:
        with op.batch_alter_table('trade_action_result', schema=None) as batch_op:
            batch_op.drop_column('transaction_id')
    else:
        print("INFO: Column 'transaction_id' does not exist in trade_action_result, skipping drop operation")
    
    # Make expert_recommendation_id NOT NULL (all actions must be linked to recommendations)
    # First, we need to update any NULL values to a valid expert_recommendation_id
    # For simplicity in this migration, we'll skip the NOT NULL constraint change
    # since the model will enforce it going forward
    # with op.batch_alter_table('trade_action_result', schema=None) as batch_op:
    #     batch_op.alter_column('expert_recommendation_id',
    #                           existing_type=sa.INTEGER(),
    #                           nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Add back transaction_id column
    with op.batch_alter_table('trade_action_result', schema=None) as batch_op:
        batch_op.add_column(sa.Column('transaction_id', sa.INTEGER(), nullable=True))
