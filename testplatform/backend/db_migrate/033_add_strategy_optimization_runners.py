"""
Migration 033: add the `strategy_optimization_runners` and `backtest_writers` tables (resume of an
interrupted job).

One row per optimization, written by the handler when the job starts: the host, pid and process
creation time of the runner (so a later start can PROVE the runner is dead before it resumes the
job, and never hijacks a live one), the code versions it started under, the interrupted row it
continues, and when the launcher's end-of-job TOP-N persist finished. `backtest_writers` records
the process writing a re-run Backtest row (top-N, out-of-sample) the same way. See
app.services.optimization_resume and docs/plans/2026-11-02-goal2027-two-phase-walk-forward.md
("Resume of an interrupted fold").

A table of its own: `strategy_optimizations` rows carry the multi-MB `all_results` blob, and code
from before this migration never reads the new table, so an old checkout keeps working on a
migrated DB and a fresh DB gets the table from `Base.metadata.create_all` (no-op here). No
existing table or row is touched. Idempotent. House pattern: 032.
"""

_DDL = """
CREATE TABLE IF NOT EXISTS strategy_optimization_runners (
    optimization_id INTEGER PRIMARY KEY REFERENCES strategy_optimizations(id),
    host VARCHAR(255) NOT NULL,
    pid INTEGER NOT NULL,
    pid_started FLOAT NOT NULL,
    proc_identity JSON,
    run_token VARCHAR(64),
    code_versions JSON,
    resumed_from_id INTEGER,
    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    topn_done_at DATETIME
);
CREATE TABLE IF NOT EXISTS backtest_writers (
    backtest_id INTEGER PRIMARY KEY REFERENCES backtests(id),
    host VARCHAR(255) NOT NULL,
    pid INTEGER NOT NULL,
    pid_started FLOAT NOT NULL,
    proc_identity JSON,
    started_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

_TABLES = ("strategy_optimization_runners", "backtest_writers")


def _table_exists(cursor, name):
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cursor.fetchone() is not None


def upgrade(cursor, conn):
    """Create the tables if absent. Returns True when something was created."""
    missing = [t for t in _TABLES if not _table_exists(cursor, t)]
    if not missing:
        print("  - strategy_optimization_runners / backtest_writers already exist; nothing to do")
        return False
    cursor.executescript(_DDL)
    conn.commit()
    print(f"  - Created {', '.join(missing)}")
    return True


def downgrade(cursor, conn):
    present = [t for t in _TABLES if _table_exists(cursor, t)]
    for t in present:
        cursor.execute(f"DROP TABLE {t}")
    conn.commit()
    return bool(present)
