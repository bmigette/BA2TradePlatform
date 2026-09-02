# Test-Platform Backend Event-Loop Stall — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the test-platform backend stay responsive while the goal2020 grid is writing to the
9.5 GB `dl_forecasting.db`, by (a) making the dashboard/list queries never read the multi-MB
backtest blobs and (b) taking blocking DB work off the asyncio event loop.

**Architecture:** Two independent fixes that compound. (1) **Covering indexes** on `backtests` and
`strategy_optimizations` that hold every column the summary/activity queries need, so SQLite serves
those queries from the index and never walks a row's overflow chain; the dashboard switches from
ORM `.all()` to narrow Core `select(...)` with `LIMIT` in SQL. (2) The routes that do blocking
SQLAlchemy/httpx work become plain `def` endpoints so FastAPI runs them in its thread pool instead
of on the single event loop; an AST test forbids reintroducing `async def` routes that never
`await`.

**Tech Stack:** FastAPI + SQLAlchemy 2 (sync, SQLite via pysqlite, `NullPool`, WAL), pytest with the
existing `client`/`db`/`gate_engine` fixtures in `testplatform/backend/tests/conftest.py`, the
house `db_migrate/NNN_*.py` migration pattern.

---

## 0. Context the implementer needs (read this first — you have zero context)

### The measured root cause (2026-09-02, live DB)

* Live DB: `C:\Users\basti\Documents\ba2\test\dl_forecasting.db` (9.57 GB, WAL). It is being
  **written concurrently by the goal2020 GA grid**. Never stop, kill, or `VACUUM` anything touching
  it. The repo-local `testplatform/backend/dl_forecasting.db` is an empty stale placeholder — ignore it.
* `backtests` has only **1,035 rows**, but the physical column order in the live DB is:
  `... 16:description, 17:status, 18:results, 19:trades, 20:equity_curve, 21:drawdown_curve,
  22:total_return ... 46:error_message, 47:is_saved, 48:created_at, 49:started_at, 50:completed_at,
  51:expert_name, 52:optimization_id, 53:adjusted_total_return, 54:labels, 55:ga_fitness`.
  The four JSON blobs average **10.8 MB per row** on recent rows. SQLite stores a row as one record;
  to read any column *after* the blobs it must walk the row's overflow-page chain sequentially
  (this DB has `auto_vacuum=0`, so there is no ptrmap shortcut). So **every query that touches
  `created_at`, `completed_at`, `expert_name`, `labels`, any metric column, etc. reads ~10 MB per
  row it visits.**
* Measured with a read-only connection, warm cache:
  * `SELECT id FROM backtests ORDER BY created_at DESC LIMIT 20` → **18.0 s** (plan: `SCAN backtests`
    + temp b-tree; there is no index on `created_at`, and reading it walks the blobs of all 1,035 rows).
  * `SELECT id FROM backtests ORDER BY id DESC LIMIT 20` → **0.000 s**.
  * `SELECT id, status FROM backtests` → 0.06 s (both columns before the blobs).
  * `SELECT id, name, status, expert_name FROM backtests` → **9.5 s** (`expert_name` is after the blobs).
  * The `list_backtests`-shaped query (`GET /api/backtests`) → **13.2 s**.
  * `strategy_optimizations` has the same shape (`all_results` avg 0.5 MB, max 3.3 MB, before
    `created_at`); 303 rows → 0.3 s today, grows with every grid job.
* `GET /api/dashboard/stats` (`app/api/dashboard.py:186`) does
  `db.query(Backtest).order_by(Backtest.created_at.desc()).limit(20).all()` — the 18 s scan **plus**
  loading 20 × 10 MB full ORM rows. Cold cache: ~2 minutes (observed 16:08:18 → 16:10:23 in
  `uvicorn_console.log`).
* `get_dashboard_stats`, `list_tasks` (`app/api/tasks.py:339`) and `list_backtests`
  (`app/api/backtests.py:175`) are **`async def`** but do only blocking sync work (SQLAlchemy, and
  `worker_fleet.refresh_remote_status` → sync `httpx`). Uvicorn runs one process, one event loop,
  so that 2-minute query freezes **every** route, including `/docs`. Two `py-spy dump`s 90 s apart
  showed `MainThread` parked inside `do_execute` at `dashboard.py:186`; all 11 task-queue worker
  threads were idle (they are *not* the problem). The frontend polls `/dashboard/stats` every 5 s
  (`frontend/src/pages/Dashboard.tsx:95`) and `/tasks?status=running` every 2–3 s, so once one poll
  stalls the backend looks permanently dead.

### Environment facts

* Python for the test platform on this machine: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe`
  (the backend `CLAUDE.md` says `./venv/bin/python`; that is the Linux path — use the one above).
* Run backend tests from `testplatform/backend`:
  `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/<file>.py -v`
  `tests/conftest.py` already points `DATABASE_URL` at a throwaway sqlite for the run and provides
  `gate_engine` (session-scoped engine with `Base.metadata.create_all`), `db` (session on it,
  rolled back per test) and `client` (`TestClient` with `get_db` overridden to the same engine).
  Rows seeded through `db` are visible to routes called through `client`.
* Migrations: `testplatform/backend/db_migrate/NNN_name.py` exposing `upgrade(cursor, conn) -> bool`
  and `downgrade`. `scripts/migrate_db.py` auto-discovers them, records applied names in
  `_migrations`, and is run **at backend startup by `app/main.py:252` with a 30-second subprocess
  timeout**. House style + tests: see `db_migrate/030_add_backtest_ga_fitness.py` and
  `tests/test_migration_025.py` (load via `importlib`, run `upgrade` on a raw `sqlite3` connection).
* SQLAlchemy's default index name for `Column(..., index=True)` is `ix_<table>_<column>`; explicit
  `Index("name", "col", ...)` in `__table_args__` is the house style for composite indexes
  (`app/models/news_cache.py:59`).
* **Versioning:** this change is entirely under `testplatform/`, so bump `TEST_APP_VERSION` in
  `testplatform/version.py` (currently `2026.09.0010`) by +1 **once, in the final commit before
  push**. Do not touch `ba2_trade_platform/version.py`. Other sessions may bump in the meantime —
  bump from whatever the value is at that time.
* Commit attribution: end every commit message with the `Co-Authored-By:` / `Claude-Session:`
  footer your session instructs you to use.
* Work in a **git worktree** on a branch off `dev` (Task 1). The `ba2_common` etc. packages are
  installed in the venv from the main checkout; this plan does not touch `packages/`, so that is fine.

### What this plan deliberately does NOT do (follow-ups, not now)

* Moving the blob columns into a side table (`backtest_payloads`). That is the trap-free design —
  every future `ALTER TABLE ADD COLUMN` lands *after* the blobs again — but it is a multi-GB data
  migration on a live DB the grid is writing to. Do it in a separate plan when the grid is idle.
* Rewriting the task-queue pollers. They sleep properly (`task_queue.py:553/557`); not the cause.
* Any frontend change.

---

## Task 1: Worktree + branch

**Step 1: Create the worktree from `dev`**

```bash
cd C:/Users/basti/Documents/dev/BA2TradePlatform
git worktree add ../BA2-dashboard-stall -b fix/testplatform-dashboard-event-loop dev
cd ../BA2-dashboard-stall/testplatform/backend
```

**Step 2: Confirm the existing suite baseline for the files you will touch**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_backtests_api_filters.py tests/test_migration_025.py -q`
Expected: all PASS (this is the baseline; if it is red before you change anything, stop and report).

---

## Task 2: Covering indexes — model declarations + migration 031

**Files:**
- Modify: `testplatform/backend/app/models/backtest.py` (imports at line 5; add `__table_args__` after line 174)
- Modify: `testplatform/backend/app/models/strategy_optimization.py` (imports at line 5; add `__table_args__` after line 44)
- Create: `testplatform/backend/db_migrate/031_add_covering_summary_indexes.py`
- Test: `testplatform/backend/tests/test_migration_031.py`

The index column lists are the single source of truth for "which columns may a summary query
touch". Put `created_at` FIRST so `ORDER BY created_at DESC` walks the index in order and a
`LIMIT 20` stops after 20 entries.

**Step 1: Write the failing tests**

```python
# testplatform/backend/tests/test_migration_031.py
"""Migration 031: covering indexes for the backtests / strategy_optimizations summary queries.

WHY. In the live DB the JSON blobs (results/trades/equity_curve/drawdown_curve; ~10 MB per recent
row) sit physically BEFORE created_at and before every column added by a later migration
(expert_name, labels, ga_fitness, ...). SQLite reads a row's overflow chain sequentially, so any
query that touches a post-blob column pays ~10 MB per row visited: ORDER BY created_at over 1,035
rows measured 18 s warm / ~2 min cold and froze the API event loop. A covering index holding
exactly the summary columns lets SQLite answer those queries from the index without touching rows.

The model declares the same indexes (__table_args__) so a FRESH DB gets them from create_all; this
migration builds them on an EXISTING DB. The parity test below pins the two column lists together.
"""
import importlib.util
import sqlite3
from pathlib import Path

MIGRATION_PATH = Path(__file__).resolve().parent.parent / "db_migrate" / "031_add_covering_summary_indexes.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_031", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _index_columns(cursor, index_name):
    cursor.execute(f"PRAGMA index_info({index_name})")
    return [row[2] for row in sorted(cursor.fetchall())]  # (seqno, cid, name) ordered by seqno


def _fresh_schema_db(tmp_path):
    """A sqlite file with the FULL model schema (what a fresh install gets from create_all)."""
    from sqlalchemy import create_engine
    from app.models.database import Base
    import app.models  # noqa: F401 — registers every model on Base.metadata

    path = tmp_path / "m031.sqlite"
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    eng.dispose()
    return path


def test_migration_column_lists_match_the_model_indexes():
    """The migration must build EXACTLY what the model declares, or fresh and upgraded DBs diverge."""
    from app.models.backtest import Backtest
    from app.models.strategy_optimization import StrategyOptimization

    m = _load_migration()
    declared = {ix.name: [c.name for c in ix.columns]
                for model in (Backtest, StrategyOptimization)
                for ix in model.__table__.indexes}
    for table, name, cols in m._INDEXES:
        assert name in declared, f"{name} is not declared on the {table} model"
        assert declared[name] == cols, f"{name}: migration columns != model columns"


def test_summary_index_starts_with_created_at_and_covers_the_list_columns():
    m = _load_migration()
    by_name = {name: cols for _t, name, cols in m._INDEXES}
    bt = by_name["ix_backtests_summary"]
    assert bt[0] == "created_at"
    # every column app/api/backtests.py:list_backtests selects from `b` must be covered
    for col in ("id", "name", "model_id", "prediction_dataset_id", "execution_dataset_id",
                "strategy_id", "start_date", "end_date", "initial_capital", "fitness_metric",
                "status", "total_return", "sharpe_ratio", "max_drawdown", "win_rate",
                "profit_factor", "total_trades", "winning_trades", "losing_trades",
                "avg_trade_duration", "final_equity", "best_trade", "worst_trade",
                "error_message", "is_saved", "created_at", "completed_at", "expert_name",
                "optimization_id", "engine_type", "description", "labels", "started_at"):
        assert col in bt, f"{col} missing from ix_backtests_summary"
    for blob in ("results", "trades", "equity_curve", "drawdown_curve"):
        assert blob not in bt
    so = by_name["ix_strategy_optimizations_activity"]
    assert so[0] == "created_at"
    assert "all_results" not in so


def test_upgrade_builds_the_indexes_on_an_existing_db_without_them(tmp_path):
    path = _fresh_schema_db(tmp_path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    # simulate a populated pre-031 DB: tables exist, indexes do not
    cur.execute("DROP INDEX ix_backtests_summary")
    cur.execute("DROP INDEX ix_strategy_optimizations_activity")
    conn.commit()

    m = _load_migration()
    assert m.upgrade(cur, conn) is True
    for _table, name, cols in m._INDEXES:
        assert _index_columns(cur, name) == cols
    conn.close()


def test_upgrade_is_a_noop_on_a_fresh_db_that_already_has_them(tmp_path):
    path = _fresh_schema_db(tmp_path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    m = _load_migration()
    assert m.upgrade(cur, conn) is False
    assert m.upgrade(cur, conn) is False  # idempotent
    conn.close()


def test_upgrade_skips_when_the_table_is_missing(tmp_path):
    conn = sqlite3.connect(tmp_path / "empty.sqlite")
    cur = conn.cursor()
    m = _load_migration()
    assert m.upgrade(cur, conn) is False
    conn.close()
```

**Step 2: Run the tests to verify they fail**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_migration_031.py -v`
Expected: FAIL — `FileNotFoundError` / `AttributeError` loading the migration, and `KeyError:
'ix_backtests_summary'` (model does not declare it yet).

**Step 3: Declare the indexes on the models**

In `app/models/backtest.py` change line 5 to:

```python
from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, ForeignKey, Float, Boolean, Index
```

and insert after the timestamps (after line 174, before `def __repr__`):

```python
    # Covering index for every SUMMARY read of this table (dashboard activity, GET /api/backtests).
    #
    # WHY. The four JSON blobs above (results/trades/equity_curve/drawdown_curve; ~10 MB per
    # recent row in the live DB) are stored physically BEFORE created_at — and before every
    # column any later migration appended (expert_name, labels, ga_fitness, ...). SQLite reads a
    # row's overflow chain sequentially, so touching ANY post-blob column costs ~10 MB per row
    # visited: `ORDER BY created_at` over ~1k rows measured 18 s warm / ~2 min cold and froze the
    # API. With every summary column in one index, SQLite serves those queries from the index
    # alone ("USING COVERING INDEX") and never opens a row.
    #
    # RULES. (1) created_at stays FIRST (newest-first ORDER BY walks the index in order).
    # (2) Never add a blob/curve column here. (3) If you add a column that a list/summary query
    # will SELECT, add it here AND to db_migrate/031 (tests pin the two lists together), or that
    # query silently falls back to the 10-MB-per-row scan. Mirrored by
    # db_migrate/031_add_covering_summary_indexes.py for existing DBs.
    __table_args__ = (
        Index(
            "ix_backtests_summary",
            "created_at", "id", "name", "engine_type", "status", "expert_name",
            "optimization_id", "labels", "model_id", "prediction_dataset_id",
            "execution_dataset_id", "strategy_id", "start_date", "end_date", "initial_capital",
            "fitness_metric", "description", "is_saved", "error_message", "started_at",
            "completed_at", "total_return", "adjusted_total_return", "ga_fitness",
            "sharpe_ratio", "max_drawdown", "win_rate", "profit_factor", "total_trades",
            "winning_trades", "losing_trades", "avg_trade_duration", "final_equity",
            "best_trade", "worst_trade",
        ),
    )
```

In `app/models/strategy_optimization.py` change line 5 to:

```python
from sqlalchemy import Column, Integer, String, DateTime, JSON, Float, ForeignKey, Index
```

and insert after line 44 (`completed_at = ...`), before `def __repr__`:

```python
    # Covering index for the dashboard's "recent optimizations" activity + status counts. Same
    # trap as backtests: all_results (0.5 MB avg, 3.3 MB max) is stored BEFORE created_at, so an
    # ORDER BY created_at scan reads every row's blob. See Backtest.__table_args__ for the rules;
    # mirrored by db_migrate/031.
    __table_args__ = (
        Index("ix_strategy_optimizations_activity",
              "created_at", "id", "name", "status", "started_at", "completed_at"),
    )
```

**Step 4: Write the migration**

```python
# testplatform/backend/db_migrate/031_add_covering_summary_indexes.py
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
```

**Step 5: Run the tests to verify they pass**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_migration_031.py -v`
Expected: 5 PASS.

**Step 6: Make sure nothing else broke on model import**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_backtests_api_filters.py tests/test_migration_025.py tests/test_migration_030.py -q` (skip 030 if the file does not exist)
Expected: PASS.

**Step 7: Commit**

```bash
git add app/models/backtest.py app/models/strategy_optimization.py db_migrate/031_add_covering_summary_indexes.py tests/test_migration_031.py
git commit -m "perf(testplatform): covering indexes so summary queries never walk the backtest blobs"
```

---

## Task 3: Dashboard — narrow, SQL-limited activity queries

**Files:**
- Modify: `testplatform/backend/app/api/dashboard.py:182-220` (the two `db.query(...).all()` blocks) + add two helpers above the route
- Test: `testplatform/backend/tests/test_dashboard_hot_path.py`

The route currently does `db.query(Backtest).order_by(...).limit(20).all()` — that loads FULL ORM
rows (all four blobs) for the 20 hits, on top of the scan. Replace with SQLAlchemy Core
`select()` of only the covered columns. Keep `LIMIT` in SQL (it already is — the point is the
column list). Expose the two statements as module constants so tests can `EXPLAIN` them.

**Step 1: Write the failing tests**

```python
# testplatform/backend/tests/test_dashboard_hot_path.py
"""GET /api/dashboard/stats must never read the backtest blobs and must be served by the covering
indexes (db_migrate/031). Background: the ORM `.all()` it used loaded 20 x ~10 MB rows after an
18 s / 2-min scan, on the event loop, freezing every route (2026-09-02 diagnosis).
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest
from sqlalchemy import event

_BLOB_RE = re.compile(r"\b(trades|equity_curve|drawdown_curve|results|all_results)\b", re.I)


@pytest.fixture
def captured_sql(gate_engine):
    """Every SQL statement executed on the gate engine during the test."""
    seen: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(gate_engine, "before_cursor_execute", _capture)
    try:
        yield seen
    finally:
        event.remove(gate_engine, "before_cursor_execute", _capture)


def _seed_backtests(db, n):
    from app.models.backtest import Backtest
    rows = []
    for i in range(n):
        bt = Backtest(
            name=f"bt-{i}", engine_type="daily_expert", status="completed",
            start_date=datetime(2020, 1, 1), end_date=datetime(2020, 6, 1),
            initial_capital=10000.0,
            trades=[{"symbol": "AAPL", "pnl": 1.0}] * 50,      # the blobs the query must not read
            equity_curve=[{"equity": 1.0}] * 50,
            drawdown_curve=[{"drawdown": 0.0}] * 50,
            results={"x": 1},
        )
        db.add(bt)
        rows.append(bt)
    db.commit()
    return rows


def _seed_optimizations(db, n):
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    s = Strategy(name="dash-opt-strategy", entry_rules=[], exit_rules=[])
    db.add(s)
    db.commit()
    rows = []
    for i in range(n):
        o = StrategyOptimization(strategy_id=s.id, name=f"opt-{i}", status="completed",
                                 fitness_metric="sharpe", optimization_type="genetic",
                                 all_results=[{"fitness": 1.0}] * 50)
        db.add(o)
        rows.append(o)
    db.commit()
    return rows


def test_recent_backtests_selects_only_covered_columns_and_limits_in_sql(db, captured_sql):
    from app.api.dashboard import _recent_backtests
    seeded = _seed_backtests(db, 25)
    captured_sql.clear()

    rows = _recent_backtests(db, limit=20)

    assert len(rows) == 20
    assert [r.id for r in rows] == sorted((b.id for b in seeded), reverse=True)[:20]
    sql = "\n".join(captured_sql)
    assert not _BLOB_RE.search(sql), sql
    assert re.search(r"\bLIMIT\b", sql, re.I), sql
    r = rows[0]
    for attr in ("id", "name", "engine_type", "status", "created_at", "started_at", "completed_at"):
        assert hasattr(r, attr)


def test_recent_optimizations_selects_only_covered_columns_and_limits_in_sql(db, captured_sql):
    from app.api.dashboard import _recent_optimizations
    seeded = _seed_optimizations(db, 25)
    captured_sql.clear()

    rows = _recent_optimizations(db, limit=20)

    assert len(rows) == 20
    assert [r.id for r in rows] == sorted((o.id for o in seeded), reverse=True)[:20]
    sql = "\n".join(captured_sql)
    assert not _BLOB_RE.search(sql), sql
    assert re.search(r"\bLIMIT\b", sql, re.I), sql


def _plan(engine, compiled_sql):
    with engine.connect() as c:
        raw = c.connection.dbapi_connection  # the sqlite3 connection
        cur = raw.cursor()
        cur.execute("EXPLAIN QUERY PLAN " + compiled_sql)
        return " | ".join(str(r) for r in cur.fetchall())


def test_recent_backtests_is_served_by_the_covering_index(gate_engine):
    from app.api.dashboard import _RECENT_BACKTESTS_SQL
    plan = _plan(gate_engine, _RECENT_BACKTESTS_SQL)
    assert "COVERING INDEX ix_backtests_summary" in plan, plan
    assert "TEMP B-TREE" not in plan, plan  # created_at leads the index: no sort step


def test_recent_optimizations_is_served_by_the_covering_index(gate_engine):
    from app.api.dashboard import _RECENT_OPTIMIZATIONS_SQL
    plan = _plan(gate_engine, _RECENT_OPTIMIZATIONS_SQL)
    assert "COVERING INDEX ix_strategy_optimizations_activity" in plan, plan
    assert "TEMP B-TREE" not in plan, plan


def test_dashboard_stats_route_never_touches_the_blobs(client, db, captured_sql):
    bts = _seed_backtests(db, 3)
    opts = _seed_optimizations(db, 2)
    captured_sql.clear()

    resp = client.get("/api/dashboard/stats")

    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["recentActivity"]}
    assert f"backtest-{bts[-1].id}" in ids
    assert f"opt-{opts[-1].id}" in ids
    assert resp.json()["jobStats"]["completed"] >= 2
    sql = "\n".join(captured_sql)
    assert not _BLOB_RE.search(sql), sql
```

**Step 2: Run the tests to verify they fail**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_dashboard_hot_path.py -v`
Expected: `ImportError: cannot import name '_recent_backtests'` for the helper tests; the route
test FAILS on the blob regex (`backtests.trades` etc. appear in the ORM SELECT).

**Step 3: Implement the helpers and use them in the route**

In `app/api/dashboard.py`, add after the imports (line 19) :

```python
from sqlalchemy import select
from app.models.strategy_optimization import StrategyOptimization

# Activity queries select ONLY columns held by the covering indexes (db_migrate/031 /
# Backtest.__table_args__), never the blobs, with LIMIT in SQL. A full-row ORM load here read
# 20 x ~10 MB rows after an 18 s / 2-min scan and froze the event loop (2026-09-02). Compiled
# strings are exposed so tests can EXPLAIN the exact statements.
_RECENT_BACKTESTS_STMT = (
    select(Backtest.id, Backtest.name, Backtest.engine_type, Backtest.status,
           Backtest.created_at, Backtest.started_at, Backtest.completed_at)
    .order_by(Backtest.created_at.desc())
)
_RECENT_OPTIMIZATIONS_STMT = (
    select(StrategyOptimization.id, StrategyOptimization.name, StrategyOptimization.status,
           StrategyOptimization.created_at, StrategyOptimization.started_at,
           StrategyOptimization.completed_at)
    .order_by(StrategyOptimization.created_at.desc())
)
_RECENT_BACKTESTS_SQL = str(_RECENT_BACKTESTS_STMT.limit(20).compile(
    compile_kwargs={"literal_binds": True}))
_RECENT_OPTIMIZATIONS_SQL = str(_RECENT_OPTIMIZATIONS_STMT.limit(20).compile(
    compile_kwargs={"literal_binds": True}))


def _recent_backtests(db: Session, limit: int):
    """Newest `limit` backtests as lightweight Row objects (no blobs)."""
    return db.execute(_RECENT_BACKTESTS_STMT.limit(limit)).all()


def _recent_optimizations(db: Session, limit: int):
    """Newest `limit` strategy optimizations as lightweight Row objects (no all_results)."""
    return db.execute(_RECENT_OPTIMIZATIONS_STMT.limit(limit)).all()
```

Then replace the two blocks in the route:

```python
    # Add backtest activities (both legacy ML runs and Phase-2 daily expert runs).
    # The engine_type discriminator (migration 018) lets us label which engine produced
    # each run so daily multi-asset expert backtests surface alongside ML backtests.
    try:
        for bt in _recent_backtests(db, limit=20):
            engine_type = (bt.engine_type or "ml")
            kind = "Daily expert backtest" if engine_type == "daily_expert" else "Backtest"
            ts = (bt.completed_at or bt.started_at or bt.created_at)
            activities.append(ActivityItem(
                id=f"backtest-{bt.id}",
                type="backtest",
                action=bt.status or "pending",
                title=f"{kind} '{bt.name}'",
                timestamp=ts.isoformat() if ts else datetime.now().isoformat(),
                status=bt.status or "pending",
            ))
    except Exception as e:
        logger.warning(f"Could not fetch backtests for activity: {e}")

    # Add strategy/expert optimization activities (the GA optimizer jobs themselves).
    try:
        _OPT_ACTION = {"running": "started", "completed": "completed", "failed": "failed"}
        for opt in _recent_optimizations(db, limit=20):
            st = opt.status or "pending"
            ts = (opt.completed_at or opt.started_at or opt.created_at)
            activities.append(ActivityItem(
                id=f"opt-{opt.id}",
                type="job",
                action=_OPT_ACTION.get(st, "created"),
                title=f"Optimization '{opt.name or ('#' + str(opt.id))}'",
                timestamp=ts.isoformat() if ts else datetime.now().isoformat(),
                status=st,
            ))
    except Exception as e:
        logger.warning(f"Could not fetch strategy optimizations for activity: {e}")
```

Also delete the now-redundant local `from app.models.strategy_optimization import
StrategyOptimization` at lines 105 and 204 (module-level import replaces them). The status
`GROUP BY` at line 106 is fine as is (`status` is a leading column and is in the activity index).

**Step 4: Run the tests to verify they pass**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_dashboard_hot_path.py -v`
Expected: 5 PASS.

If `test_recent_backtests_is_served_by_the_covering_index` fails with a plan like
`SCAN backtests` + `USE TEMP B-TREE`, SQLite did not pick the index: print the plan, check the
index exists on the gate engine (`PRAGMA index_list(backtests)`), and check every selected column
is in the index. Do NOT "fix" it by adding `INDEXED BY` (it errors when the index is absent on an
un-migrated DB).

**Step 5: Commit**

```bash
git add app/api/dashboard.py tests/test_dashboard_hot_path.py
git commit -m "perf(dashboard): activity queries select only covered columns, LIMIT in SQL"
```

---

## Task 4: `GET /api/backtests` list must be index-served too

**Files:**
- Modify (probably nothing): `testplatform/backend/app/api/backtests.py:175-296`
- Test: `testplatform/backend/tests/test_backtests_list_plan.py`

`list_backtests` already uses raw SQL restricted to summary columns (its author hit the same blob
problem, see the comment at line 201) — but it `ORDER BY b.created_at` and selects 8 post-blob
columns, so today it walks every blob (13.2 s). With Task 2's index it should be fully covered.
This task only proves it and guards it.

**Step 1: Write the failing-or-passing test**

```python
# testplatform/backend/tests/test_backtests_list_plan.py
"""GET /api/backtests must be answered from ix_backtests_summary (db_migrate/031), never by a row
scan: with ~10 MB of blobs per row in the live DB the un-indexed list measured 13 s and ran on the
event loop. Captures the statement the route actually emits and EXPLAINs it on the gate engine.
"""
from __future__ import annotations

import pytest
from sqlalchemy import event


@pytest.fixture
def captured_sql(gate_engine):
    seen: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(gate_engine, "before_cursor_execute", _capture)
    try:
        yield seen
    finally:
        event.remove(gate_engine, "before_cursor_execute", _capture)


def test_list_backtests_query_is_served_by_the_covering_index(client, gate_engine, captured_sql):
    resp = client.get("/api/backtests")
    assert resp.status_code == 200, resp.text

    list_sql = [s for s in captured_sql if "FROM backtests b" in s]
    assert len(list_sql) == 1, captured_sql
    with gate_engine.connect() as c:
        cur = c.connection.dbapi_connection.cursor()
        cur.execute("EXPLAIN QUERY PLAN " + list_sql[0])
        plan = " | ".join(str(r) for r in cur.fetchall())
    assert "COVERING INDEX ix_backtests_summary" in plan, plan
    assert "TEMP B-TREE" not in plan, plan
```

**Step 2: Run it**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_backtests_list_plan.py -v`
Expected: PASS if the index covers every `b.` column the route selects. If it FAILS, the plan
string tells you which is wrong: a column missing from the index → add it to BOTH the model
`Index` and `_INDEXES` in migration 031 (Task 2's parity test keeps them equal), never by
dropping the column from the SELECT.

**Step 3: Commit**

```bash
git add tests/test_backtests_list_plan.py
git commit -m "test(backtests): pin GET /api/backtests to the covering index"
```

---

## Task 5: Hot-path routes off the event loop

**Files:**
- Modify: `testplatform/backend/app/api/dashboard.py:73` (`async def get_dashboard_stats` → `def`)
- Modify: `testplatform/backend/app/api/tasks.py:339` (`async def list_tasks` → `def`)
- Modify: `testplatform/backend/app/api/backtests.py:175` (`async def list_backtests` → `def`)
- Test: `testplatform/backend/tests/test_routes_do_not_block_event_loop.py`

FastAPI runs a plain `def` endpoint in its thread pool (anyio, 40 threads), so a slow one delays
only itself; an `async def` endpoint that does blocking work stalls the single event loop for
every client. None of these three awaits anything, so the change is deleting the `async` keyword.
Dependencies like `db: Session = Depends(get_db)` (a sync generator) already run in the thread
pool and keep working.

**Step 1: Write the failing test**

```python
# testplatform/backend/tests/test_routes_do_not_block_event_loop.py
"""An `async def` route that never awaits does blocking work ON the event loop: one slow DB call
in it stalls every other request (observed 2026-09-02: /api/dashboard/stats froze /docs for two
minutes). FastAPI runs plain `def` routes in a thread pool, so the rule is: a route is `async def`
only if it actually awaits.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

# Polled by the frontend every 2-5 s; the routes that took the backend down.
HOT_PATHS = {
    ("GET", "/api/dashboard/stats"),
    ("GET", "/api/tasks"),
    ("GET", "/api/backtests"),
}


def _api_routes():
    from fastapi.routing import APIRoute
    from app.main import app
    return [r for r in app.routes if isinstance(r, APIRoute)]


def _awaits_something(fn) -> bool:
    src = textwrap.dedent(inspect.getsource(inspect.unwrap(fn)))
    tree = ast.parse(src)
    return any(isinstance(n, (ast.Await, ast.AsyncWith, ast.AsyncFor)) for n in ast.walk(tree))


def _pure_blocking_async_routes():
    bad = []
    for r in _api_routes():
        if inspect.iscoroutinefunction(r.endpoint) and not _awaits_something(r.endpoint):
            for m in sorted(r.methods):
                bad.append(f"{m} {r.path} -> {r.endpoint.__module__}.{r.endpoint.__name__}")
    return bad


def test_hot_path_routes_are_plain_def():
    by_key = {(m, r.path): r for r in _api_routes() for m in r.methods}
    for key in HOT_PATHS:
        assert key in by_key, f"route {key} not found — did its path change?"
        assert not inspect.iscoroutinefunction(by_key[key].endpoint), \
            f"{key} is async def but does only blocking work; make it `def`"


def test_no_async_route_does_only_blocking_work():
    """Every remaining `async def` route must genuinely await. Fix = delete `async` on the
    listed endpoints (Task 6 of docs/plans/2026-09-02-testplatform-backend-event-loop-stall.md)."""
    assert _pure_blocking_async_routes() == []
```

**Step 2: Run the hot-path test to verify it fails**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_routes_do_not_block_event_loop.py::test_hot_path_routes_are_plain_def -v`
Expected: FAIL — `('GET', '/api/dashboard/stats') is async def but does only blocking work`.

**Step 3: Make the three routes plain `def`**

Delete the `async ` prefix on exactly these three definitions:

```python
# app/api/dashboard.py:73
def get_dashboard_stats(db: Session = Depends(get_db)):

# app/api/tasks.py:339
def list_tasks(

# app/api/backtests.py:175
def list_backtests(
```

Add one comment above `get_dashboard_stats` (the worst offender) so nobody "fixes" it back:

```python
# Plain `def` ON PURPOSE: this handler does blocking SQLAlchemy + sync httpx (remote worker
# /health probes) work. As `async def` it ran on the event loop and a single slow query froze
# every route for minutes (2026-09-02). tests/test_routes_do_not_block_event_loop.py pins this.
```

**Step 4: Run the hot-path test + the route tests that exercise these three**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_routes_do_not_block_event_loop.py::test_hot_path_routes_are_plain_def tests/test_dashboard_hot_path.py tests/test_backtests_api_filters.py tests/test_backtests_list_plan.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add app/api/dashboard.py app/api/tasks.py app/api/backtests.py tests/test_routes_do_not_block_event_loop.py
git commit -m "fix(api): run the polled dashboard/tasks/backtests routes off the event loop"
```

---

## Task 6: Sweep — every other `async def` route that never awaits

**Files:**
- Modify: whichever files `test_no_async_route_does_only_blocking_work` lists (expected: most
  routes in `app/api/backtests.py`, `strategies.py`, `jobs.py`, `indicator_collections.py`,
  `target_sets.py`, `ml.py`, `rules.py`, `datasets.py`, `models.py`, `workers.py`, `tasks.py`,
  `tools.py`, `settings.py`, `admin.py`, `cache.py`, `data_build.py`, `experts.py`, `ruleset_meta.py`)

**Step 1: Get the list**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_routes_do_not_block_event_loop.py::test_no_async_route_does_only_blocking_work -v`
Expected: FAIL with the full list of `METHOD /path -> module.function` lines. That list is the
work item; nothing else changes.

**Step 2: For each listed function, delete the `async ` keyword — and nothing else**

Before touching a file, run these greps in it; anything they hit inside a listed function needs
a look, not a blind edit:

```bash
grep -n "asyncio\.\|get_event_loop\|create_task\|run_in_threadpool\|await \|async with\|async for" app/api/<file>.py
```

* A route that awaits is NOT on the list (the test skips it) — leave it alone.
* `BackgroundTasks`, `Request`, `Response`, `HTTPException`, `Depends(get_db)` all work
  identically in a `def` route.
* WebSocket routes are `@router.websocket` and await — not on the list.

**Step 3: Run the whole test + the existing API tests**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_routes_do_not_block_event_loop.py tests/test_rules_api.py tests/test_robustness_api.py tests/test_optimize_route.py tests/test_backtests_api_filters.py tests/test_cache_api.py tests/test_experts_api.py tests/test_ruleset_meta.py tests/test_backtest_labels.py -v`
Expected: all PASS. If a converted route's test fails, that route does something loop-bound
inside a callee (e.g. `asyncio.get_event_loop()`); revert that one to `async def`, wrap its
blocking part in `await run_in_threadpool(...)` from `fastapi.concurrency`, and note it in the
commit message.

**Step 4: Commit**

```bash
git add app/api/
git commit -m "fix(api): routes that never await are plain def, so they run in the thread pool"
```

---

## Task 7: Full backend suite, version bump, push

**Step 1: Run the full backend suite**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest -q` (from `testplatform/backend`; takes a few minutes)
Expected: green, apart from failures you can show are pre-existing on `dev` (compare with a run
on the main checkout if in doubt — report them, do not fix unrelated things).

**Step 2: Bump the test-platform version**

In `testplatform/version.py` set `TEST_APP_VERSION` to the current value + 1 (e.g.
`"2026.09.0010"` → `"2026.09.0011"`; read the file first — other sessions may have bumped it).

**Step 3: Commit and push the branch**

```bash
git add testplatform/version.py
git commit -m "chore(testplatform): bump TEST_APP_VERSION for the event-loop stall fix"
git push -u origin fix/testplatform-dashboard-event-loop
```

Then merge into `dev` the way this repo does (`git checkout dev && git merge --no-ff fix/... &&
git push`, from the main checkout) — or open a PR if the user prefers; ask.

---

## Task 8: Live rollout (the human/operator step — do it in this order)

The backend on this machine is currently **stopped** (port 8000 free). The grid is still writing
to the live DB and must not be interrupted.

**Step 1: Apply migration 031 manually, backend not running**

From the merged `dev` checkout, `testplatform/backend`:

```bash
C:\Users\basti\ba2-venvs\test\Scripts\python.exe scripts/migrate_db.py --status   # 031 should be PENDING
C:\Users\basti\ba2-venvs\test\Scripts\python.exe scripts/migrate_db.py            # builds both indexes
```

Expected output includes `Created ix_backtests_summary on backtests (35 columns)`. Building it
reads every row's post-blob columns once — expect **~20 s warm to a few minutes cold** — and
holds the SQLite write lock the whole time, so grid writes will wait/retry (top-N persist has
6 retries / 155 s of budget; `strategy_optimizations` progress updates retry on their next tick).
Prefer a moment when the grid log shows it between generations. If it is killed midway, nothing
is committed — just rerun. Do NOT let `app/main.py`'s 30-second auto-migrate be the thing that
tries it (that is why this step comes before starting the backend).

**Step 2: Verify the index is used on the live DB (read-only, ~instant)**

```bash
C:\Users\basti\ba2-venvs\test\Scripts\python.exe -c "
import sqlite3,time
c=sqlite3.connect('file:C:/Users/basti/Documents/ba2/test/dl_forecasting.db?mode=ro',uri=True)
q='SELECT id,name,engine_type,status,created_at,started_at,completed_at FROM backtests ORDER BY created_at DESC LIMIT 20'
print(c.execute('EXPLAIN QUERY PLAN '+q).fetchall())
t=time.time(); c.execute(q).fetchall(); print(f'{time.time()-t:.3f}s')"
```

Expected: plan mentions `COVERING INDEX ix_backtests_summary`, time well under 0.05 s (was 18 s).

**Step 3: Start the backend and prove it stays up**

```bash
cd testplatform/backend
nohup C:/Users/basti/ba2-venvs/test/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > uvicorn_console.log 2>&1 &
```

Then for two minutes (the frontend on :5173 will start polling as soon as it is open):

```bash
for i in $(seq 1 40); do curl -sS -m 4 -o /dev/null -w "$(date +%T) /docs %{http_code} %{time_total}s\n" http://localhost:8000/docs; sleep 3; done
curl -sS -m 30 -o /dev/null -w "dashboard %{http_code} %{time_total}s\n" http://localhost:8000/api/dashboard/stats
curl -sS -m 30 -o /dev/null -w "backtests %{http_code} %{time_total}s\n" "http://localhost:8000/api/backtests"
```

Expected: every `/docs` probe 200 in ~0.2 s with no gaps; `/api/dashboard/stats` well under 5 s
(it still live-probes two remote workers at 1.5 s each); `/api/backtests` well under 1 s (was 13 s).
`grep "Duration" uvicorn_console.log | sort -t: -k4 -n | tail` shows no multi-second responses.
If anything still stalls: `C:\Users\basti\ba2-venvs\test\Scripts\py-spy.exe dump --pid <PID>` —
`MainThread` must never be inside `sqlalchemy` frames now.
