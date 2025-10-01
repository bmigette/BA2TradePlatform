"""fix_tradingorder_autoincrement

Revision ID: 0d97964e8ad8
Revises: rename_order_rec_to_expert
Create Date: 2025-10-01 15:20:46.561091

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0d97964e8ad8'
down_revision: Union[str, Sequence[str], None] = 'rename_order_rec_to_expert'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - fix tradingorder table to have AUTOINCREMENT on id."""
    
    conn = op.get_bind()
    conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
    
    # Create new table with proper AUTOINCREMENT
    conn.execute(sa.text("""
        CREATE TABLE tradingorder_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            account_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            good_for TEXT,
            status TEXT NOT NULL,
            filled_qty REAL,
            comment TEXT,
            created_at DATETIME,
            open_type TEXT NOT NULL DEFAULT 'MANUAL',
            broker_order_id TEXT,
            expert_recommendation_id INTEGER,
            limit_price REAL,
            stop_price REAL,
            depends_on_order INTEGER,
            depends_order_status_trigger TEXT,
            transaction_id INTEGER,
            FOREIGN KEY(account_id) REFERENCES accountdefinition(id) ON DELETE CASCADE,
            FOREIGN KEY(expert_recommendation_id) REFERENCES expertrecommendation(id),
            FOREIGN KEY(depends_on_order) REFERENCES tradingorder(id),
            FOREIGN KEY(transaction_id) REFERENCES "transaction"(id) ON DELETE CASCADE
        )
    """))
    
    # Copy data from old table to new table
    conn.execute(sa.text("""
        INSERT INTO tradingorder_new (
            id, account_id, symbol, quantity, side, order_type, good_for, status,
            filled_qty, comment, created_at, open_type, broker_order_id,
            expert_recommendation_id, limit_price, stop_price, depends_on_order,
            depends_order_status_trigger, transaction_id
        )
        SELECT 
            id, account_id, symbol, quantity, side, order_type, good_for, status,
            filled_qty, comment, created_at, open_type, broker_order_id,
            expert_recommendation_id, limit_price, stop_price, depends_on_order,
            depends_order_status_trigger, transaction_id
        FROM tradingorder
    """))
    
    # Drop old table
    conn.execute(sa.text("DROP TABLE tradingorder"))
    
    # Rename new table
    conn.execute(sa.text("ALTER TABLE tradingorder_new RENAME TO tradingorder"))
    
    conn.execute(sa.text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    """Downgrade schema - revert to non-AUTOINCREMENT id (not recommended)."""
    
    conn = op.get_bind()
    conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
    
    # Create table without AUTOINCREMENT
    conn.execute(sa.text("""
        CREATE TABLE tradingorder_old (
            id INTEGER PRIMARY KEY NOT NULL,
            account_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            good_for TEXT,
            status TEXT NOT NULL,
            filled_qty REAL,
            comment TEXT,
            created_at DATETIME,
            open_type TEXT NOT NULL DEFAULT 'MANUAL',
            broker_order_id TEXT,
            expert_recommendation_id INTEGER,
            limit_price REAL,
            stop_price REAL,
            depends_on_order INTEGER,
            depends_order_status_trigger TEXT,
            transaction_id INTEGER
        )
    """))
    
    # Copy data
    conn.execute(sa.text("""
        INSERT INTO tradingorder_old
        SELECT * FROM tradingorder
    """))
    
    # Drop new table
    conn.execute(sa.text("DROP TABLE tradingorder"))
    
    # Rename old table
    conn.execute(sa.text("ALTER TABLE tradingorder_old RENAME TO tradingorder"))
    
    conn.execute(sa.text("PRAGMA foreign_keys=ON"))
