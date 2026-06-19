"""
Migration 018: Make backtests.model_id nullable + add engine_type discriminator

Phase 2 (daily expert backtest engine) introduces non-ML backtests driven by the
packaged ba2trade expert -> recommendation -> order path. These runs are NOT
model-driven, so they persist with model_id=NULL. The legacy ML path always sets a
real model_id. To let one shared `backtests` table carry both engines we:

  1. Ensure `model_id` is NULLABLE.
     SQLite cannot ALTER a column's NOT NULL constraint in place, so when an existing
     (pre-Phase-2) DB still declares `model_id INTEGER NOT NULL` we rebuild the table
     (create-new / copy / drop / rename) preserving every row. On a DB already created
     with the current SQLAlchemy model (model_id already nullable) this rebuild is
     skipped and we only add the new column.

  2. Add `engine_type VARCHAR DEFAULT 'ml'` (values: 'ml' | 'daily_expert') so the UI
     and queries can distinguish the two engines. Existing rows are stamped 'ml' (they
     are all legacy ML runs). The daily route sets engine_type='daily_expert'.

Idempotent: re-running detects the column / nullability and no-ops.
Gate: applies on a fresh DB AND upgrades an existing populated DB with no data loss.
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def _model_id_is_not_null(cursor):
    """Return True if the `model_id` column is declared NOT NULL on the live table."""
    cursor.execute("PRAGMA table_info(backtests)")
    for cid, name, ctype, notnull, dflt, pk in cursor.fetchall():
        if name == "model_id":
            # PRAGMA table_info `notnull` is 1 when the column has a NOT NULL constraint.
            return bool(notnull)
    return False


def _table_exists(cursor, name):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cursor.fetchone() is not None


def _rebuild_table_with_nullable_model_id(cursor, conn):
    """
    Rebuild `backtests` so `model_id` becomes nullable, preserving all rows.

    SQLite has no `ALTER COLUMN`, so the canonical recipe is:
      create new table (with the desired column defs) -> copy rows -> drop old -> rename.
    We read the existing column list dynamically so any extra columns added by earlier
    migrations (010/012) are carried across automatically and no data is lost.
    """
    cols = get_table_columns(cursor, "backtests")

    # New schema mirrors app/models/backtest.py (model_id nullable, engine_type present).
    # We only need to correct the model_id nullability + add engine_type here; every
    # other column is copied verbatim from the existing table definition. To keep the
    # rebuild faithful to whatever the old DB had we reuse its column list and types.
    cursor.execute("PRAGMA table_info(backtests)")
    old_info = cursor.fetchall()  # (cid, name, type, notnull, dflt_value, pk)

    col_defs = []
    for cid, name, ctype, notnull, dflt, pk in old_info:
        ctype = ctype or ""
        parts = [f'"{name}"', ctype]
        if pk:
            parts.append("PRIMARY KEY")
        if name == "model_id":
            # Force nullable: do NOT emit NOT NULL.
            pass
        elif notnull:
            parts.append("NOT NULL")
        if dflt is not None:
            parts.append(f"DEFAULT {dflt}")
        col_defs.append(" ".join(p for p in parts if p))

    # Ensure engine_type is part of the rebuilt schema if it was not already present.
    has_engine_type = "engine_type" in cols
    if not has_engine_type:
        col_defs.append('"engine_type" VARCHAR DEFAULT \'ml\'')

    new_table_sql = (
        "CREATE TABLE backtests_new (\n  "
        + ",\n  ".join(col_defs)
        + "\n)"
    )
    cursor.execute(new_table_sql)

    # Copy data for the shared columns (old column set).
    col_list = ", ".join(f'"{c}"' for c in cols)
    cursor.execute(
        f"INSERT INTO backtests_new ({col_list}) SELECT {col_list} FROM backtests"
    )

    cursor.execute("DROP TABLE backtests")
    cursor.execute("ALTER TABLE backtests_new RENAME TO backtests")
    # Recreate the index SQLAlchemy declares (ix_backtests_id).
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_backtests_id ON backtests (id)")
    print("  - Rebuilt backtests table: model_id is now nullable")
    if not has_engine_type:
        print("  - Added engine_type column during rebuild (default 'ml')")


def upgrade(cursor, conn):
    """Make model_id nullable + add engine_type discriminator."""
    if not _table_exists(cursor, "backtests"):
        # Fresh DB where the table hasn't been created yet (init_db/create_all will
        # build it from the SQLAlchemy model, which already has model_id nullable +
        # engine_type). Nothing to migrate.
        print("  - backtests table does not exist yet; nothing to migrate")
        return False

    added = False
    columns = get_table_columns(cursor, "backtests")
    needs_nullable_rebuild = _model_id_is_not_null(cursor)

    if needs_nullable_rebuild:
        # Rebuild path: also brings engine_type in if missing.
        _rebuild_table_with_nullable_model_id(cursor, conn)
        added = True
        columns = get_table_columns(cursor, "backtests")
    else:
        print("  - model_id is already nullable; no table rebuild needed")

    # Add engine_type if it is still missing (non-rebuild path, or rebuild that kept it).
    if "engine_type" not in columns:
        cursor.execute(
            "ALTER TABLE backtests ADD COLUMN engine_type VARCHAR DEFAULT 'ml'"
        )
        print("  - Added engine_type column to backtests table (default 'ml')")
        added = True
    else:
        print("  - engine_type column already exists")

    # Stamp existing rows that have no engine_type yet as legacy ML runs.
    cursor.execute(
        "UPDATE backtests SET engine_type = 'ml' WHERE engine_type IS NULL"
    )
    if cursor.rowcount:
        print(f"  - Stamped {cursor.rowcount} existing rows with engine_type='ml'")
        added = True

    if added:
        conn.commit()
    return added


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN / nullability changes easily; no-op."""
    print("  - Downgrade not supported for this migration")
    return False
