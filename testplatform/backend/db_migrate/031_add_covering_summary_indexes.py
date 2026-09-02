"""
Migration 031: covering indexes for the backtests / strategy_optimizations summary queries.

Adds two indexes (no columns):

  ix_backtests_summary                 (created_at, id, name, engine_type, status, ...)
  ix_strategy_optimizations_activity   (created_at, id, name, status, started_at, completed_at)

WHY. In a populated DB the JSON blobs on backtests (results/trades/equity_curve/drawdown_curve,
~10 MB per recent row) are stored physically BEFORE created_at and before every column a later
migration appended. SQLite reads a row's overflow chain sequentially, so any query touching a
post-blob column pays ~10 MB per row it visits: the dashboard's newest-20 query measured 18 s warm
and ~2 minutes cold on a 9.5 GB DB, and — running on the API's event loop — froze every route.
strategy_optimizations has the same shape (all_results before created_at). A covering index that
holds exactly the summary columns lets SQLite answer those queries from the index without opening
a row. The SAME indexes are declared on the models (Backtest / StrategyOptimization
__table_args__) so a fresh DB gets them from create_all; tests pin the two column lists together.

OPERATIONAL NOTE. Building ix_backtests_summary on an existing large DB must read every row's
post-blob columns ONCE (= walk every blob chain, several GB, ~20 s warm to a few minutes cold)
while holding SQLite's single write lock, so concurrent writers wait up to their busy_timeout.
app/main.py runs this script at startup with a 30-second subprocess timeout; on a large DB that
kills the build before it commits (harmless: the transaction is discarded, but it is retried on
every start). APPLY IT MANUALLY FIRST on a large DB:
    python scripts/migrate_db.py          # from testplatform/backend, backend NOT running
Idempotent (CREATE INDEX IF NOT EXISTS + index_list check). Follows the 017-030 house pattern.
"""

# (table, index name, ordered columns) — MUST equal the model's Index() declarations
# (tests/test_migration_031.py::test_migration_column_lists_match_the_model_indexes).
_INDEXES = [
    ("backtests", "ix_backtests_summary", [
        "created_at", "id", "name", "engine_type", "status", "expert_name",
        "optimization_id", "labels", "model_id", "prediction_dataset_id",
        "execution_dataset_id", "strategy_id", "start_date", "end_date", "initial_capital",
        "fitness_metric", "description", "is_saved", "error_message", "started_at",
        "completed_at", "total_return", "adjusted_total_return", "ga_fitness",
        "sharpe_ratio", "max_drawdown", "win_rate", "profit_factor", "total_trades",
        "winning_trades", "losing_trades", "avg_trade_duration", "final_equity",
        "best_trade", "worst_trade",
    ]),
    ("strategy_optimizations", "ix_strategy_optimizations_activity", [
        "created_at", "id", "name", "status", "started_at", "completed_at",
    ]),
]


def _table_exists(cursor, name):
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cursor.fetchone() is not None


def _index_exists(cursor, table, name):
    cursor.execute(f"PRAGMA index_list({table})")
    return any(row[1] == name for row in cursor.fetchall())


def _table_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def upgrade(cursor, conn):
    """Create the covering indexes that are not already present."""
    added = False
    for table, name, cols in _INDEXES:
        if not _table_exists(cursor, table):
            print(f"  - {table} table does not exist yet; skipping {name}")
            continue
        if _index_exists(cursor, table, name):
            print(f"  - {name} already exists; nothing to do")
            continue
        missing = [c for c in cols if c not in _table_columns(cursor, table)]
        if missing:
            # Earlier migrations add these; if they have not run this one must not half-build.
            print(f"  - {table} is missing columns {missing}; skipping {name}")
            continue
        col_sql = ", ".join(cols)
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({col_sql})")
        print(f"  - Created {name} on {table} ({len(cols)} columns)")
        added = True

    if added:
        conn.commit()
    return added


def downgrade(cursor, conn):
    """Drop the two indexes (safe: they hold no data of their own)."""
    dropped = False
    for table, name, _cols in _INDEXES:
        if _table_exists(cursor, table) and _index_exists(cursor, table, name):
            cursor.execute(f"DROP INDEX {name}")
            print(f"  - Dropped {name}")
            dropped = True
    if dropped:
        conn.commit()
    return dropped
