"""
Migration 022: drop the retired `Strategy.rm_*` columns.

Risk Manager params are now optimized as expert settings, so the 5 RM params'
25 columns on the `strategies` table are dead:

  rm_risk_per_trade_pct, rm_per_instrument_cap_pct, rm_min_stop_pct,
  rm_atr_stop_mult, rm_max_concurrent_positions

  ...each with the matching _optimize / _min / _max / _step columns.

sqlite cannot reliably DROP COLUMN across versions, so we rebuild the table.
The rebuild is SCHEMA-PRESERVING: we recreate `strategies_new` with explicit DDL
that mirrors the current SQLAlchemy `Strategy` model -- keeping
`id INTEGER PRIMARY KEY AUTOINCREMENT`, `name VARCHAR(255) NOT NULL`, the JSON
columns, the initial_tp_*/initial_sl_* columns with their defaults, and the
timestamps -- then copy the kept columns' data over, drop the old table, rename
the new one into place, and recreate the model's index(es).

A naive `CREATE TABLE ... AS SELECT` (CTAS) would have produced an untyped,
PK-less, AUTOINCREMENT-less table, breaking ORM inserts and id uniqueness; the
explicit DDL below avoids that.

Idempotent: a no-op (returns False) when there are no `rm_*` columns, so it is
safe on a FRESH DB (SQLAlchemy create_all already builds `strategies` without
rm_* columns) and on a re-run. The ML path never used these columns.
Follows the 017-021 house pattern: `upgrade(cursor, conn)` returning truthy when
changes were applied.
"""

# Explicit DDL for the rebuilt table, mirroring app/models/strategy.py.
# Keeping this in sync with the model is what preserves the PK/AUTOINCREMENT,
# NOT NULL constraints, and column defaults that a CTAS would silently drop.
STRATEGIES_DDL = """
CREATE TABLE strategies_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    required_fields JSON,
    entry_conditions JSON,
    buy_entry_conditions JSON,
    sell_entry_conditions JSON,
    exit_conditions JSON,
    initial_tp_percent FLOAT DEFAULT 5.0,
    initial_tp_optimize BOOLEAN DEFAULT 0,
    initial_tp_min FLOAT,
    initial_tp_max FLOAT,
    initial_tp_step FLOAT,
    initial_sl_percent FLOAT DEFAULT 2.0,
    initial_sl_optimize BOOLEAN DEFAULT 0,
    initial_sl_min FLOAT,
    initial_sl_max FLOAT,
    initial_sl_step FLOAT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME
)
"""

# Indexes declared on the model (Strategy.id has index=True).
STRATEGIES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_strategies_id ON strategies (id)",
]


def get_table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def _table_exists(cursor, name):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cursor.fetchone() is not None


def upgrade(cursor, conn):
    """Rebuild `strategies` keeping only the non-`rm_` columns + their data.

    Schema-preserving: the new table is created from explicit DDL mirroring the
    ORM model, so the PRIMARY KEY/AUTOINCREMENT, NOT NULL, and defaults survive.
    """
    if not _table_exists(cursor, "strategies"):
        print("  - strategies table does not exist yet; nothing to migrate")
        return False

    columns = get_table_columns(cursor, "strategies")
    rm_columns = [c for c in columns if c.startswith("rm_")]
    if not rm_columns:
        print("  - no rm_* columns on strategies; nothing to migrate")
        return False

    kept = [c for c in columns if not c.startswith("rm_")]
    kept_csv = ", ".join(kept)
    print(f"  - dropping {len(rm_columns)} rm_* columns from strategies")

    # SQLite-safe, schema-preserving table rebuild:
    #   1. create strategies_new with the real ORM schema (PK/AUTOINCREMENT etc.)
    #   2. copy the kept columns' data (incl. id) into it
    #   3. drop the old table and rename the new one into place
    #   4. recreate the model's index(es)
    cursor.execute("DROP TABLE IF EXISTS strategies_new")
    cursor.execute(STRATEGIES_DDL)
    cursor.execute(
        f"INSERT INTO strategies_new ({kept_csv}) "
        f"SELECT {kept_csv} FROM strategies"
    )
    cursor.execute("DROP TABLE strategies")
    cursor.execute("ALTER TABLE strategies_new RENAME TO strategies")
    for index_sql in STRATEGIES_INDEXES:
        cursor.execute(index_sql)

    conn.commit()
    print(f"  - rebuilt strategies with {len(kept)} columns (PK/AUTOINCREMENT preserved)")
    return True


def downgrade(cursor, conn):
    """SQLite has no simple DROP COLUMN; downgrade is a no-op (data is gone)."""
    print("  - Downgrade not supported for this migration")
    return False
