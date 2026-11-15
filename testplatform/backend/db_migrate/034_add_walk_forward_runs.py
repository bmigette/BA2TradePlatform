"""
Migration 034: add the `walk_forward_runs` table (walk-forward results in the DB, for the UI).

One row per walk-forward chain, identified by (cell name, report-directory lane), so two lanes using the
same cell name keep separate rows (one goal2027 cell, or one run created in the UI): its spec, status,
progress and the full per-fold report as JSON. Written by app.services.wf_store while the chain runs
(in addition to the report files, which are unchanged) and by tools/import_wf_reports.py for a report
directory produced elsewhere. See app.models.walk_forward_run.

Every starting state converges on the same schema, idempotently: no table (created), a table from an
EARLIER version of this migration (each missing column added with ALTER TABLE ADD COLUMN: no table
rewrite, existing rows keep NULL), or the current shape (nothing to do). A fresh DB gets the same
columns from `Base.metadata.create_all` (pinned by tests/test_migration_034.py), and `init_db()` runs
this upgrade on every start so a DB without the table converges too. No other table is touched; code
from before this migration never reads it. House pattern: 033.
"""

TABLE = "walk_forward_runs"

#: Every column, in model order: (name, SQLite declaration). The first entries are the CREATE TABLE
#: core; any column missing from an existing table is ADDED (all of them are nullable).
_COLUMNS = [
    ("id", "INTEGER PRIMARY KEY"),
    ("name", "VARCHAR(255) NOT NULL"),
    ("lane_key", "VARCHAR(1024) NOT NULL"),
    ("source", "VARCHAR(16) NOT NULL"),
    ("expert", "VARCHAR(100)"),
    ("strategy", "VARCHAR(50)"),
    ("band", "VARCHAR(50)"),
    ("status", "VARCHAR(24) NOT NULL"),
    ("planned_folds", "INTEGER"),
    ("folds_done", "INTEGER"),
    ("current_fold", "INTEGER"),
    ("progress_message", "VARCHAR(500)"),
    ("stitched_return", "FLOAT"),
    ("worst_oos_dd", "FLOAT"),
    ("stitched_trades", "INTEGER"),
    ("stitch_partial", "BOOLEAN"),
    ("error_message", "TEXT"),
    ("task_id", "VARCHAR(64)"),
    ("driver_token", "VARCHAR(64)"),
    ("driver_host", "VARCHAR(255)"),
    ("driver_pid", "INTEGER"),
    ("driver_pid_started", "FLOAT"),
    ("driver_identity", "JSON"),
    ("report_dir", "VARCHAR(1024)"),
    ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ("started_at", "DATETIME"),
    ("updated_at", "DATETIME"),
    ("completed_at", "DATETIME"),
    ("spec", "JSON"),
    ("report", "JSON"),
]

#: One row per (cell name, report-directory lane): app.models.walk_forward_run.lane_key.
_UNIQUE = "CONSTRAINT uq_walk_forward_runs_name_lane UNIQUE (name, lane_key)"

#: The core a table must already have for the ADD COLUMN path (anything else is not a shape this
#: migration knows). ``lane_key`` is core: it carries the uniqueness, which ALTER TABLE cannot add.
_CORE = {"id", "name", "lane_key", "source", "status"}

_INDEX = "CREATE INDEX IF NOT EXISTS ix_walk_forward_runs_id ON walk_forward_runs (id)"


def _table_exists(cursor, name):
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cursor.fetchone() is not None


def _columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def upgrade(cursor, conn):
    """Create the table or add its missing columns. Returns True when something changed."""
    changed = []
    if not _table_exists(cursor, TABLE):
        cursor.execute(f"CREATE TABLE {TABLE} (\n    "
                       + ",\n    ".join([f"{n} {d}" for n, d in _COLUMNS] + [_UNIQUE]) + "\n)")
        changed.append(f"created {TABLE}")
    else:
        have = _columns(cursor, TABLE)
        missing_core = sorted(_CORE - have)
        if missing_core:
            raise RuntimeError(f"{TABLE} lacks core column(s) {missing_core}: not a shape this "
                               f"migration knows; inspect the table before migrating")
        for name, decl in _COLUMNS:
            if name not in have:
                # SQLite's ADD COLUMN refuses a non-constant default: an added created_at stays NULL.
                decl = decl.replace(" DEFAULT CURRENT_TIMESTAMP", "")
                cursor.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {decl}")
                changed.append(f"added {TABLE}.{name}")
    cursor.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_walk_forward_runs_id'")
    if cursor.fetchone() is None:
        cursor.execute(_INDEX)
        changed.append("created index ix_walk_forward_runs_id")
    conn.commit()
    print("  - " + ("; ".join(changed) if changed else f"{TABLE} up to date; nothing to do"))
    return bool(changed)


def downgrade(cursor, conn):
    present = _table_exists(cursor, TABLE)
    if present:
        cursor.execute(f"DROP TABLE {TABLE}")
    conn.commit()
    return present
