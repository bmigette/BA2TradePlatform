"""
Migration 020: add classic Risk Manager optimize columns to the `strategies` table
(Phase 4 joint genetic optimizer, Decision 4).

The Phase-4 optimizer searches over expert + classic-RM + ruleset/condition params in
ONE joint space. The five classic-RM params each need a baseline value column plus
optimization-range columns, mirroring the existing TP/SL pattern on the Strategy model
(`initial_tp_*` / `initial_sl_*`):

  risk_per_trade_pct      (FLOAT)   - % of equity risked per trade
  per_instrument_cap_pct  (FLOAT)   - max virtual equity % per instrument
  min_stop_pct            (FLOAT)   - floor on the stop-loss distance %
  atr_stop_mult           (FLOAT)   - ATR multiplier for stop placement
  max_concurrent_positions(INTEGER) - cap on simultaneously open positions

Each param gets five columns: `rm_<p>` (baseline value), `rm_<p>_optimize` (BOOLEAN),
`rm_<p>_min` / `rm_<p>_max` / `rm_<p>_step` (range bounds). The four percent/multiplier
params use FLOAT for value/min/max/step; max_concurrent_positions uses INTEGER. These are
the schema half of `strategy_param_space._collect_rm`; the optimization handler builds
`rm_cfg` from them.

Idempotent: every ADD COLUMN is guarded by a `get_table_columns` check, so re-running is a
no-op. Gate: applies on a FRESH DB (where init_db/create_all already built `strategies`
with these columns from the SQLAlchemy model -> all guards skip) AND upgrades an EXISTING
populated DB (adds only the missing columns, never touches existing rows). Follows the
013/014 house pattern (db_migrate NNN_*.py auto-discovered by scripts/migrate_db.py).
"""

# (name, sqlite_type) for every classic-RM column, in model declaration order.
# FLOAT for the four percent/multiplier params + all *_optimize as BOOLEAN;
# INTEGER for max_concurrent_positions value/min/max/step.
_RM_COLUMNS = [
    ("rm_risk_per_trade_pct", "FLOAT"),
    ("rm_risk_per_trade_pct_optimize", "BOOLEAN"),
    ("rm_risk_per_trade_pct_min", "FLOAT"),
    ("rm_risk_per_trade_pct_max", "FLOAT"),
    ("rm_risk_per_trade_pct_step", "FLOAT"),
    ("rm_per_instrument_cap_pct", "FLOAT"),
    ("rm_per_instrument_cap_pct_optimize", "BOOLEAN"),
    ("rm_per_instrument_cap_pct_min", "FLOAT"),
    ("rm_per_instrument_cap_pct_max", "FLOAT"),
    ("rm_per_instrument_cap_pct_step", "FLOAT"),
    ("rm_min_stop_pct", "FLOAT"),
    ("rm_min_stop_pct_optimize", "BOOLEAN"),
    ("rm_min_stop_pct_min", "FLOAT"),
    ("rm_min_stop_pct_max", "FLOAT"),
    ("rm_min_stop_pct_step", "FLOAT"),
    ("rm_atr_stop_mult", "FLOAT"),
    ("rm_atr_stop_mult_optimize", "BOOLEAN"),
    ("rm_atr_stop_mult_min", "FLOAT"),
    ("rm_atr_stop_mult_max", "FLOAT"),
    ("rm_atr_stop_mult_step", "FLOAT"),
    ("rm_max_concurrent_positions", "INTEGER"),
    ("rm_max_concurrent_positions_optimize", "BOOLEAN"),
    ("rm_max_concurrent_positions_min", "INTEGER"),
    ("rm_max_concurrent_positions_max", "INTEGER"),
    ("rm_max_concurrent_positions_step", "INTEGER"),
]


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def _table_exists(cursor, name):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cursor.fetchone() is not None


def upgrade(cursor, conn):
    """Add the classic-RM optimize columns to `strategies` if not already present."""
    if not _table_exists(cursor, "strategies"):
        # Fresh DB where the table hasn't been created yet (init_db/create_all will
        # build it from the SQLAlchemy model, which already declares every rm_* column).
        # Nothing to migrate.
        print("  - strategies table does not exist yet; nothing to migrate")
        return False

    existing = set(get_table_columns(cursor, "strategies"))
    added = False
    for name, sqlite_type in _RM_COLUMNS:
        if name not in existing:
            cursor.execute(
                f"ALTER TABLE strategies ADD COLUMN {name} {sqlite_type}"
            )
            print(f"  - Added {name} ({sqlite_type}) to strategies table")
            added = True

    if added:
        conn.commit()
    else:
        print("  - All classic-RM columns already exist; nothing to migrate")
    return added


def downgrade(cursor, conn):
    """SQLite has no simple DROP COLUMN across versions; downgrade is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
