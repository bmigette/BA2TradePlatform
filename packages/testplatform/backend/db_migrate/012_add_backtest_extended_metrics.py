"""
Migration 012: Add extended metrics columns to backtests table

Adds additional performance metrics from backtesting.py library:
- Risk metrics: sortino_ratio, calmar_ratio, volatility
- Trade quality: sqn, expectancy, avg_trade
- Drawdown details: avg_drawdown, max_drawdown_duration
- Benchmark: buy_hold_return, annualized_return
- Position: exposure_time, equity_peak
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add new extended metric columns to backtests table."""
    columns = get_table_columns(cursor, "backtests")
    added = False

    # Define new columns to add (column_name, sql_type)
    new_columns = [
        # Risk metrics
        ("exposure_time", "REAL"),  # % of time in position
        ("buy_hold_return", "REAL"),  # Benchmark B&H return
        ("annualized_return", "REAL"),  # Annualized return %
        ("volatility", "REAL"),  # Annualized volatility %
        ("sortino_ratio", "REAL"),  # Downside risk-adjusted return
        ("calmar_ratio", "REAL"),  # Return / Max Drawdown

        # Trade quality metrics
        ("sqn", "REAL"),  # System Quality Number
        ("expectancy", "REAL"),  # Average expected return per trade
        ("avg_drawdown", "REAL"),  # Average drawdown %
        ("max_drawdown_duration", "REAL"),  # Max DD duration in days
        ("avg_trade", "REAL"),  # Average trade return % (geometric)
        ("equity_peak", "REAL"),  # Peak equity reached
    ]

    for col_name, col_type in new_columns:
        if col_name not in columns:
            cursor.execute(f"ALTER TABLE backtests ADD COLUMN {col_name} {col_type}")
            print(f"  - Added {col_name} column to backtests table")
            added = True
        else:
            print(f"  - {col_name} column already exists")

    if added:
        conn.commit()
    return added


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
