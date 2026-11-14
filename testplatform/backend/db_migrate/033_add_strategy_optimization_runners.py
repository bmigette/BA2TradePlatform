"""
Migration 033: add the `strategy_optimization_runners` table (resume of an interrupted job).

One row per optimization, written by the handler when the job starts: the host, pid and process
creation time of the runner (so a later start can PROVE the runner is dead before it resumes the
job, and never hijacks a live one), the code versions it started under, the interrupted row it
continues, and when the launcher's end-of-job TOP-N persist finished. See
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
    code_versions JSON,
    resumed_from_id INTEGER,
    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    topn_done_at DATETIME
);
"""


def _table_exists(cursor, name):
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cursor.fetchone() is not None


def upgrade(cursor, conn):
    """Create the table if absent. Returns True when something was created."""
    if _table_exists(cursor, "strategy_optimization_runners"):
        print("  - strategy_optimization_runners already exists; nothing to do")
        return False
    cursor.executescript(_DDL)
    conn.commit()
    print("  - Created strategy_optimization_runners")
    return True


def downgrade(cursor, conn):
    if not _table_exists(cursor, "strategy_optimization_runners"):
        return False
    cursor.execute("DROP TABLE strategy_optimization_runners")
    conn.commit()
    return True
