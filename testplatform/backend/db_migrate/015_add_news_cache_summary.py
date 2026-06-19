"""
Migration 015: Add summary column to news_cache table

Stores the original provider summary separately from full article content
(which is stored in files on disk). This allows FinBERT to use both
the provider summary and full content for sentiment analysis.
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add summary column to news_cache table."""
    columns = get_table_columns(cursor, "news_cache")

    if "summary" not in columns:
        cursor.execute("ALTER TABLE news_cache ADD COLUMN summary TEXT DEFAULT NULL")
        print("  - Added summary column to news_cache table")
        conn.commit()
        return True
    else:
        print("  - summary column already exists")
        return False


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
