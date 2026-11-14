"""
Migration 033: add the `strategy_optimization_runners` and `backtest_writers` tables (resume of an
interrupted job).

One row per optimization, written when the job is created and when its handler starts: the host,
pid, process creation time and independent process identity of the runner (so a later start can
PROVE the runner is dead before it resumes the job, and never hijacks a live one), its run token,
the code versions, the interrupted row it continues, when the launcher's end-of-job TOP-N persist
finished and when the handler returned. `backtest_writers` records the process writing a re-run
Backtest row (top-N, out-of-sample) the same way. See app.services.optimization_resume and
docs/plans/2026-11-02-goal2027-two-phase-walk-forward.md ("Resume of an interrupted fold").

Every starting state converges on the same schema, idempotently: no table (both created), a table
from an EARLIER version of this migration (each missing column added with ALTER TABLE ADD COLUMN:
no table rewrite, existing rows keep NULL), or the current shape (nothing to do). A fresh DB gets
the same columns from `Base.metadata.create_all` (pinned by tests/test_migration_033.py). Code from
before this migration never reads these tables. No other table is touched. House pattern: 032.
"""

#: Every column of the two tables, in model order: (name, SQLite declaration). The first entries
#: of each table are its CREATE TABLE core; any column missing from an existing table is ADDED.
_COLUMNS = {
    "strategy_optimization_runners": [
        ("optimization_id", "INTEGER PRIMARY KEY REFERENCES strategy_optimizations(id)"),
        ("host", "VARCHAR(255) NOT NULL"),
        ("pid", "INTEGER NOT NULL"),
        ("pid_started", "FLOAT NOT NULL"),
        ("proc_identity", "JSON"),
        ("run_token", "VARCHAR(64)"),
        ("code_versions", "JSON"),
        ("resumed_from_id", "INTEGER"),
        ("started_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
        ("topn_done_at", "DATETIME"),
        ("ended_at", "DATETIME"),
    ],
    "backtest_writers": [
        ("backtest_id", "INTEGER PRIMARY KEY REFERENCES backtests(id)"),
        ("host", "VARCHAR(255) NOT NULL"),
        ("pid", "INTEGER NOT NULL"),
        ("pid_started", "FLOAT NOT NULL"),
        ("proc_identity", "JSON"),
        ("started_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ],
}

# Columns a later version added: nullable and without a constant-needing default, so ALTER TABLE ADD
# COLUMN accepts them on a table that already holds rows.
_ADDABLE = {"proc_identity", "run_token", "ended_at"}


def _table_exists(cursor, name):
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cursor.fetchone() is not None


def _columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def upgrade(cursor, conn):
    """Create missing tables, add missing columns. Returns True when something changed."""
    changed = []
    for table, cols in _COLUMNS.items():
        if not _table_exists(cursor, table):
            cursor.execute(f"CREATE TABLE {table} (\n    "
                           + ",\n    ".join(f"{n} {d}" for n, d in cols) + "\n)")
            changed.append(f"created {table}")
            continue
        have = _columns(cursor, table)
        for name, decl in cols:
            if name in have:
                continue
            if name not in _ADDABLE:
                raise RuntimeError(f"{table} lacks core column {name!r}: not a shape this migration "
                                   f"knows; inspect the table before migrating")
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            changed.append(f"added {table}.{name}")
    conn.commit()
    print("  - " + ("; ".join(changed) if changed else
                    "strategy_optimization_runners / backtest_writers up to date; nothing to do"))
    return bool(changed)


def downgrade(cursor, conn):
    present = [t for t in _COLUMNS if _table_exists(cursor, t)]
    for t in present:
        cursor.execute(f"DROP TABLE {t}")
    conn.commit()
    return bool(present)
