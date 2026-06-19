"""
Migration 019: add the `screener_history` table (Phase 3 Task 5).

Phase 3 introduces the survivorship-free as-of stock screener. Per scan date the screener
emits a grouped/labeled survivor set keyed `(symbol, scan_date, screen_config_hash)`, which
is persisted so a backtest sweep over scan dates becomes a cheap cache replay (cache-once,
zero provider fetches on replay).

`screener_history` is a dedicated host table (Decision 7) — NOT a SQLAlchemy model and NOT
part of `Base.metadata` (the cache service uses raw sqlite3 via
`app/services/screener_history_cache.py`). Because `init_db()` (Base.metadata.create_all)
will never create it, this migration owns its creation in the host DB schema lifecycle.

The CREATE TABLE / index here is kept BYTE-IDENTICAL to `screener_history_cache._DDL` so the
service's standalone self-create (executescript on construction) and the host migration
converge on exactly one schema.

Idempotent: CREATE TABLE/INDEX IF NOT EXISTS + a table-exists short-circuit, so re-running
is a no-op. Gate: applies on a FRESH DB and on an EXISTING populated DB with no data loss
(it only adds a new table, never touches existing tables/rows).
"""

# Kept identical to app/services/screener_history_cache.py::_DDL.
_DDL = """
CREATE TABLE IF NOT EXISTS screener_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    group_label TEXT NOT NULL,
    screen_config_hash TEXT NOT NULL,
    rank INTEGER,
    sort_metric_value REAL,
    market_cap_as_of REAL,
    price_as_of REAL,
    relative_volume REAL,
    weinstein_stage INTEGER,
    price_drop_pct REAL,
    universe_mode TEXT,
    market_cap_source TEXT,
    float_approx INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(symbol, scan_date, screen_config_hash)
);
CREATE INDEX IF NOT EXISTS ix_scan_hash ON screener_history(scan_date, screen_config_hash);
"""


def _table_exists(cursor, name):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cursor.fetchone() is not None


def _index_exists(cursor, name):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,)
    )
    return cursor.fetchone() is not None


def upgrade(cursor, conn):
    """Create the `screener_history` table + its index if not already present."""
    if _table_exists(cursor, "screener_history") and _index_exists(cursor, "ix_scan_hash"):
        print("  - screener_history table + index already exist; nothing to migrate")
        return False

    cursor.executescript(_DDL)
    conn.commit()
    print("  - Created screener_history table + ix_scan_hash index")
    return True


def downgrade(cursor, conn):
    """Drop the `screener_history` table (and its index)."""
    cursor.execute("DROP INDEX IF EXISTS ix_scan_hash")
    cursor.execute("DROP TABLE IF EXISTS screener_history")
    conn.commit()
    print("  - Dropped screener_history table + ix_scan_hash index")
    return True
