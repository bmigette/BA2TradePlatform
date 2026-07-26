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
    entry_actions JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME
)
"""

# Indexes declared on the model (Strategy.id has index=True).
STRATEGIES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_strategies_id ON strategies (id)",
]


def _ddl_column_names(ddl: str):
    """Column names declared in a CREATE TABLE statement.

    Parsed from the DDL rather than hardcoded a second time, so the copy list cannot drift
    from the table actually being created."""
    import re
    body = ddl[ddl.index("(") + 1: ddl.rindex(")")]
    names = []
    for line in body.split(","):
        line = line.strip()
        if not line:
            continue
        first = line.split()[0]
        if first.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
            continue
        names.append(first)
    return names


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

    # Copy only columns present in BOTH the source table AND the rebuilt DDL (fixed
    # 2026-07-26). This used to keep every non-rm_ source column and INSERT them all, which
    # breaks the moment the source has a column the DDL lacks:
    #
    #     sqlite3.OperationalError: table strategies_new has no column named initial_tp_percent
    #
    # That is not hypothetical -- it is the state of every pre-022 database. STRATEGIES_DDL is
    # a hardcoded snapshot, and the comment above it instructs keeping it "in sync with the
    # model". It duly WAS synced forward when 026 replaced initial_tp_*/initial_sl_* with
    # entry_actions -- which silently broke 022 as a HISTORICAL migration, because a DB that
    # has not yet run 022 still carries those dead columns. Result: replaying the chain on an
    # old database (restoring a backup, cloning an old snapshot) failed hard at 022.
    #
    # Intersecting is safe here specifically because 026 DROPS initial_tp_*/initial_sl_*
    # outright rather than converting their values, so discarding them one migration earlier
    # loses nothing that would otherwise have survived. Anything dropped is printed rather
    # than silently swallowed.
    ddl_columns = set(_ddl_column_names(STRATEGIES_DDL))
    kept = [c for c in columns if not c.startswith("rm_") and c in ddl_columns]
    dropped_not_in_ddl = [c for c in columns
                          if not c.startswith("rm_") and c not in ddl_columns]
    kept_csv = ", ".join(kept)
    print(f"  - dropping {len(rm_columns)} rm_* columns from strategies")
    if dropped_not_in_ddl:
        print(f"  - also dropping {len(dropped_not_in_ddl)} column(s) absent from the rebuilt "
              f"schema (removed by a later migration): {', '.join(dropped_not_in_ddl)}")

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
