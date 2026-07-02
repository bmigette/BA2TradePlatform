"""
Migration 025: add the `robustness_runs` table (Backtest Robustness Suite).

A RobustnessRun links a parent backtest to a stress-test:
  - kind='monte_carlo': percentile bands / probabilities / drop-K table computed by pure MC
    over the parent's persisted `trades` (no new backtest rows).
  - kind='schedule': N NEW variant Backtest rows re-run with a shifted analysis day/time
    (never in place); their ids are recorded in `variant_backtest_ids`.

Unlike `screener_history` (migration 019, a raw host table), `robustness_runs` IS a
SQLAlchemy model (`app/models/backtest.py::RobustnessRun`). So on a FRESH DB
`Base.metadata.create_all()` builds the table from the model and this migration's
table-exists short-circuit makes it a no-op; on an EXISTING populated DB this migration
creates the table. Either way no existing tables/rows are touched.

The CREATE TABLE below is kept consistent with the RobustnessRun model column TYPES:
JSON columns (params/results/variant_backtest_ids) stored as SQLite JSON (TEXT affinity),
timestamps as DATETIME, `kind`/`status` as VARCHAR, backtest_id as an FK -> backtests(id).

Idempotent: CREATE TABLE/INDEX IF NOT EXISTS + a table-exists short-circuit, so re-running
is a no-op. Follows the 019 (new-table) / 017-024 house pattern.
"""

_DDL = """
CREATE TABLE IF NOT EXISTS robustness_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_id INTEGER NOT NULL REFERENCES backtests(id),
    kind VARCHAR(50) NOT NULL,
    params JSON,
    results JSON,
    variant_backtest_ids JSON,
    status VARCHAR(50) DEFAULT 'pending',
    error_message VARCHAR(1000),
    created_at DATETIME,
    completed_at DATETIME
);
CREATE INDEX IF NOT EXISTS ix_robustness_runs_backtest_id
    ON robustness_runs(backtest_id);
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
    """Create the `robustness_runs` table + its backtest_id index if not present."""
    if _table_exists(cursor, "robustness_runs") and _index_exists(
        cursor, "ix_robustness_runs_backtest_id"
    ):
        print("  - robustness_runs table + index already exist; nothing to migrate")
        return False

    cursor.executescript(_DDL)
    conn.commit()
    print("  - Created robustness_runs table + ix_robustness_runs_backtest_id index")
    return True


def downgrade(cursor, conn):
    """Drop the `robustness_runs` table (and its index)."""
    cursor.execute("DROP INDEX IF EXISTS ix_robustness_runs_backtest_id")
    cursor.execute("DROP TABLE IF EXISTS robustness_runs")
    conn.commit()
    print("  - Dropped robustness_runs table + ix_robustness_runs_backtest_id index")
    return True
