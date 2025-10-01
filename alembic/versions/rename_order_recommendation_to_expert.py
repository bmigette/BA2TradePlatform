"""Rename order_recommendation_id to expert_recommendation_id in TradingOrder and remove expert_recommendation_id from Transaction

Revision ID: rename_order_rec_to_expert
Revises: 3271f7f4e2f2
Create Date: 2025-10-01 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'rename_order_rec_to_expert'
down_revision: Union[str, None] = '3271f7f4e2f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema using raw SQL for SQLite compatibility."""
    
    conn = op.get_bind()
    
    # Step 1: Rename order_recommendation_id to expert_recommendation_id in TradingOrder
    conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
    
    # Create a temporary table with the new column name
    conn.execute(sa.text("""
        CREATE TABLE tradingorder_new AS SELECT 
            id, symbol, quantity, side, order_type, good_for, status, filled_qty,
            created_at, comment, order_recommendation_id AS expert_recommendation_id,
            open_type, transaction_id, broker_order_id, limit_price,
            depends_on_order, depends_order_status_trigger, stop_price, account_id
        FROM tradingorder
    """))
    
    # Drop the old table
    conn.execute(sa.text("DROP TABLE tradingorder"))
    
    # Rename the new table
    conn.execute(sa.text("ALTER TABLE tradingorder_new RENAME TO tradingorder"))
    
    # Step 2: Remove expert_recommendation_id from Transaction if it exists
    try:
        # Check if the column exists
        result = conn.execute(sa.text("PRAGMA table_info(transaction)"))
        columns = [row[1] for row in result]
        
        if 'expert_recommendation_id' in columns:
            # Get the columns for transaction table
            result = conn.execute(sa.text("PRAGMA table_info(transaction)"))
            trans_columns = [row[1] for row in result if row[1] != 'expert_recommendation_id']
            cols_str = ', '.join(trans_columns)
            
            # Create a new transaction table without expert_recommendation_id
            conn.execute(sa.text(f"""
                CREATE TABLE transaction_new AS SELECT 
                    {cols_str}
                FROM transaction
            """))
            
            # Drop the old table
            conn.execute(sa.text("DROP TABLE transaction"))
            
            # Rename the new table
            conn.execute(sa.text("ALTER TABLE transaction_new RENAME TO transaction"))
    except Exception as e:
        # If transaction table doesn't have the column, that's fine
        pass
    
    conn.execute(sa.text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    """Downgrade schema using raw SQL for SQLite compatibility."""
    
    conn = op.get_bind()
    conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
    
    # Rename expert_recommendation_id back to order_recommendation_id in TradingOrder
    conn.execute(sa.text("""
        CREATE TABLE tradingorder_new AS SELECT 
            id, symbol, quantity, side, order_type, good_for, status, filled_qty,
            created_at, comment, expert_recommendation_id AS order_recommendation_id,
            open_type, transaction_id, broker_order_id, limit_price,
            depends_on_order, depends_order_status_trigger, stop_price, account_id
        FROM tradingorder
    """))
    
    conn.execute(sa.text("DROP TABLE tradingorder"))
    conn.execute(sa.text("ALTER TABLE tradingorder_new RENAME TO tradingorder"))
    
    # Add expert_recommendation_id back to Transaction (with NULL values)
    try:
        # Get current columns
        result = conn.execute(sa.text("PRAGMA table_info(transaction)"))
        trans_columns = [row[1] for row in result]
        cols_str = ', '.join(trans_columns)
        
        conn.execute(sa.text(f"""
            CREATE TABLE transaction_new AS SELECT 
                {cols_str}, NULL as expert_recommendation_id
            FROM transaction
        """))
        
        conn.execute(sa.text("DROP TABLE transaction"))
        conn.execute(sa.text("ALTER TABLE transaction_new RENAME TO transaction"))
    except Exception:
        pass
    
    conn.execute(sa.text("PRAGMA foreign_keys=ON"))
