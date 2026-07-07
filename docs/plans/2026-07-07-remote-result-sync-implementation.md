# Remote Result Sync Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replicate `Backtest`/`StrategyOptimization` (and their `Strategy` dependency) rows from
the master's testplatform DB to every remote worker's own copy of that same DB file, so any worker
host can browse and replay past runs even without the master.

**Architecture:** A new master-side `sync_client.py` pushes full-row JSON snapshots over HTTP to
new `/sync/*` endpoints on the existing (currently DB-less) `worker_server.py`. The worker writes
through its own `app.models.database.SessionLocal` — the same file/module a full backend on that
host would use — matching rows by `(name, created_at)` rather than id, since ids aren't stable
across separate databases, and remapping cross-references (`Backtest.strategy_id`,
`Backtest.optimization_id`, `StrategyOptimization.strategy_id`) to the worker's own local ids via
the parent's natural key. Every push is fire-and-forget and sends full current state, so a missed
push self-heals on the next one.

**Tech Stack:** FastAPI, SQLAlchemy (plain, not SQLModel — this is the testplatform stack), httpx,
pytest, sqlite3 for migrations.

**Design doc:** `docs/plans/2026-07-07-remote-result-sync-design.md` — read it first if anything
below is unclear on the *why*.

---

### Task 1: `Worker.sync_results_enabled` column + migration

**Files:**
- Modify: `testplatform/backend/app/models/worker.py`
- Create: `testplatform/backend/db_migrate/027_add_worker_sync_results_enabled.py`
- Test: `testplatform/backend/tests/test_migration_027.py`

**Step 1: Write the failing migration test**

```python
"""Test for migration 027: add workers.sync_results_enabled (default True, backfilled)."""
import importlib.util
import sqlite3
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "db_migrate"
    / "027_add_worker_sync_results_enabled.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_027", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def _build_legacy_workers(conn):
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(100) NOT NULL,
            url VARCHAR(500) NOT NULL,
            is_enabled BOOLEAN DEFAULT 1
        )
        """
    )
    cursor.execute("INSERT INTO workers (name, url) VALUES (?, ?)", ("existing-worker", "http://h:8100"))
    conn.commit()


def test_migration_adds_column_and_backfills_existing_rows():
    conn = sqlite3.connect(":memory:")
    try:
        _build_legacy_workers(conn)
        cursor = conn.cursor()

        before = _table_columns(cursor, "workers")
        assert "sync_results_enabled" not in before

        migration = _load_migration()
        result = migration.upgrade(cursor, conn)
        assert result

        after = _table_columns(cursor, "workers")
        assert "sync_results_enabled" in after

        cursor.execute("SELECT sync_results_enabled FROM workers WHERE name = 'existing-worker'")
        assert cursor.fetchone()[0] == 1

    finally:
        conn.close()


def test_migration_is_idempotent():
    conn = sqlite3.connect(":memory:")
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                sync_results_enabled BOOLEAN DEFAULT 1
            )
            """
        )
        conn.commit()
        migration = _load_migration()
        result = migration.upgrade(cursor, conn)
        assert not result
    finally:
        conn.close()
```

**Step 2: Run test to verify it fails**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_migration_027.py -v`
Expected: FAIL — `027_add_worker_sync_results_enabled.py` doesn't exist yet (`FileNotFoundError` /
`ModuleNotFoundError` from `spec_from_file_location` / `exec_module`).

**Step 3: Write the migration**

```python
"""Migration 027: add workers.sync_results_enabled.

Adds one nullable-but-defaulted column for the result-replication feature:

  workers.sync_results_enabled   (BOOLEAN, default 1) - whether Backtest/StrategyOptimization
                                  rows get pushed to this worker. On by default for every
                                  worker, including ones that already existed before this
                                  migration ran (explicit UPDATE backfill below — don't rely
                                  solely on SQLite's ADD COLUMN ... DEFAULT to backfill existing
                                  rows the way a fresh INSERT would get the Python-side default).

Idempotent: the ADD COLUMN is guarded by a column check, so re-running is a no-op. Follows the
017-026 house pattern.
"""


def _table_exists(cursor, name):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cursor.fetchone() is not None


def get_table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add workers.sync_results_enabled if not already present; backfill existing rows to 1."""
    if not _table_exists(cursor, "workers"):
        print("  - workers table does not exist yet; skipping sync_results_enabled")
        return False
    if "sync_results_enabled" in set(get_table_columns(cursor, "workers")):
        print("  - workers.sync_results_enabled already exists; nothing to do")
        return False

    cursor.execute("ALTER TABLE workers ADD COLUMN sync_results_enabled BOOLEAN DEFAULT 1")
    print("  - Added sync_results_enabled (BOOLEAN) to workers")
    cursor.execute("UPDATE workers SET sync_results_enabled = 1 WHERE sync_results_enabled IS NULL")
    print("  - Backfilled sync_results_enabled = 1 for existing workers")
    conn.commit()
    return True


def downgrade(cursor, conn):
    """SQLite has no simple DROP COLUMN across versions; downgrade is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
```

**Step 4: Run test to verify it passes**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_migration_027.py -v`
Expected: PASS (2 tests)

**Step 5: Add the column to the SQLAlchemy model**

In `testplatform/backend/app/models/worker.py`, add the column next to `is_enabled` (so a fresh
`create_all()` DB and a migrated existing DB end up with the identical schema):

```python
    # Status
    is_enabled = Column(Boolean, default=True)
    is_local = Column(Boolean, default=False)
    status = Column(String(20), default="offline")  # "online", "offline", "busy"

    # Result-replication: push saved Backtest/StrategyOptimization rows to this worker so it
    # can browse/replay them independently of the master. On by default.
    sync_results_enabled = Column(Boolean, default=True)
```

And expose it in `to_dict()`:

```python
            "isEnabled": self.is_enabled,
            "isLocal": self.is_local,
            "status": self.status,
            "syncResultsEnabled": self.sync_results_enabled,
```

**Step 6: Run the full worker model test file (if one exists) plus the new migration test**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_migration_027.py -v`
Expected: PASS

**Step 7: Commit**

```bash
git add testplatform/backend/app/models/worker.py \
        testplatform/backend/db_migrate/027_add_worker_sync_results_enabled.py \
        testplatform/backend/tests/test_migration_027.py
git commit -m "feat(workers): add sync_results_enabled column, on by default"
```

---

### Task 2: Worker-side upsert-by-natural-key core logic

**Files:**
- Create: `testplatform/backend/app/services/sync_receiver.py`
- Test: `testplatform/backend/tests/test_sync_receiver.py`

This is the core matching logic from the design doc's "Matching and Upsert Semantics" section,
kept independently testable (no HTTP, no FastAPI) against a temp SQLite file.

**Step 1: Write the failing tests**

```python
"""Worker-side upsert-by-(name, created_at) logic — no HTTP involved, a temp sqlite file only."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization
from app.models.backtest import Backtest
from app.services.sync_receiver import upsert_by_natural_key


@pytest.fixture()
def session(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'worker_mirror.sqlite'}")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()
    eng.dispose()


def _iso(dt):
    return dt.isoformat()


def test_insert_when_absent(session):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    payload = {"id": 999, "name": "my-strategy", "created_at": _iso(created), "description": "d"}

    upsert_by_natural_key(session, Strategy, payload)

    rows = session.query(Strategy).all()
    assert len(rows) == 1
    assert rows[0].id != 999  # never trust the master's id
    assert rows[0].name == "my-strategy"
    assert rows[0].description == "d"


def test_update_in_place_when_name_and_created_at_match(session):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    upsert_by_natural_key(
        session, Strategy,
        {"id": 1, "name": "s1", "created_at": _iso(created), "description": "v1"},
    )
    local_id = session.query(Strategy).one().id

    upsert_by_natural_key(
        session, Strategy,
        {"id": 1, "name": "s1", "created_at": _iso(created), "description": "v2"},
    )

    rows = session.query(Strategy).all()
    assert len(rows) == 1  # no duplicate row
    assert rows[0].id == local_id  # same local row, updated in place
    assert rows[0].description == "v2"


def test_different_created_at_is_a_different_row(session):
    upsert_by_natural_key(
        session, Strategy,
        {"id": 1, "name": "s1", "created_at": _iso(datetime(2026, 1, 1, tzinfo=timezone.utc))},
    )
    upsert_by_natural_key(
        session, Strategy,
        {"id": 1, "name": "s1", "created_at": _iso(datetime(2026, 1, 2, tzinfo=timezone.utc))},
    )
    assert session.query(Strategy).count() == 2


def test_parent_fk_resolves_to_local_id_not_master_id(session):
    strat_created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    upsert_by_natural_key(
        session, Strategy,
        {"id": 42, "name": "parent-strat", "created_at": _iso(strat_created)},
    )
    local_strategy_id = session.query(Strategy).one().id
    assert local_strategy_id != 42  # prove the local id really differs from master's

    opt_created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    upsert_by_natural_key(
        session, StrategyOptimization,
        {
            "id": 7, "name": "opt-1", "created_at": _iso(opt_created),
            "strategy_id": 42,  # the MASTER's id — must NOT end up on the local row verbatim
            "strategy_name": "parent-strat", "strategy_created_at": _iso(strat_created),
            "status": "running", "fitness_metric": "sharpe", "optimization_type": "genetic",
        },
        parent_fk={"strategy_id": (Strategy, "strategy_name", "strategy_created_at")},
    )

    opt = session.query(StrategyOptimization).one()
    assert opt.strategy_id == local_strategy_id
    assert opt.strategy_id != 42


def test_required_parent_fk_missing_locally_skips_the_write(session):
    """StrategyOptimization.strategy_id is NOT NULL in the schema — unlike Backtest's FKs,
    which are nullable. If the referenced Strategy hasn't synced to this worker yet (the
    strategy-first push ordering is meant to prevent this, but a single push is fire-and-forget
    per worker, so a narrow race is possible), inserting with strategy_id=None would violate
    the column's constraint. The upsert must skip the write entirely rather than raise — the
    next full-state push (e.g. next generation) retries the parent first and self-heals."""
    opt_created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    result = upsert_by_natural_key(
        session, StrategyOptimization,
        {
            "id": 7, "name": "opt-1", "created_at": _iso(opt_created),
            "strategy_id": 42,
            "strategy_name": "never-synced", "strategy_created_at": _iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            "status": "running", "fitness_metric": "sharpe", "optimization_type": "genetic",
        },
        parent_fk={"strategy_id": (Strategy, "strategy_name", "strategy_created_at")},
    )
    assert result is None  # signals "skipped", not an error
    assert session.query(StrategyOptimization).count() == 0  # no partial/invalid row persisted


def test_nullable_parent_fk_missing_locally_resolves_to_none(session):
    """Contrast with the required-FK case above: Backtest.strategy_id IS nullable (a standalone
    backtest legitimately has no optimization/strategy row), so a missing parent resolves to
    None and the row is still written — it is not skipped."""
    bt_created = datetime(2026, 1, 3, tzinfo=timezone.utc)
    bt = upsert_by_natural_key(
        session, Backtest,
        {
            "id": 1, "name": "standalone-run", "created_at": _iso(bt_created),
            "engine_type": "daily_expert",
            "strategy_id": 42, "strategy_name": "never-synced",
            "strategy_created_at": _iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            "start_date": _iso(datetime(2024, 1, 1)), "end_date": _iso(datetime(2024, 6, 1)),
        },
        parent_fk={"strategy_id": (Strategy, "strategy_name", "strategy_created_at")},
    )
    assert bt is not None
    assert bt.strategy_id is None
    assert session.query(Backtest).count() == 1


def test_backtest_dual_parent_fk_resolution(session):
    strat_created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    upsert_by_natural_key(session, Strategy, {"id": 1, "name": "s", "created_at": _iso(strat_created)})
    local_strategy_id = session.query(Strategy).one().id

    opt_created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    upsert_by_natural_key(
        session, StrategyOptimization,
        {
            "id": 1, "name": "opt", "created_at": _iso(opt_created),
            "strategy_id": 1, "strategy_name": "s", "strategy_created_at": _iso(strat_created),
            "fitness_metric": "sharpe", "optimization_type": "genetic",
        },
        parent_fk={"strategy_id": (Strategy, "strategy_name", "strategy_created_at")},
    )
    local_opt_id = session.query(StrategyOptimization).one().id

    bt_created = datetime(2026, 1, 3, tzinfo=timezone.utc)
    upsert_by_natural_key(
        session, Backtest,
        {
            "id": 1, "name": "TOP1-opt", "created_at": _iso(bt_created),
            "engine_type": "daily_expert",
            "strategy_id": 1, "strategy_name": "s", "strategy_created_at": _iso(strat_created),
            "optimization_id": 1, "optimization_name": "opt", "optimization_created_at": _iso(opt_created),
            "start_date": _iso(datetime(2024, 1, 1)), "end_date": _iso(datetime(2024, 6, 1)),
        },
        parent_fk={
            "strategy_id": (Strategy, "strategy_name", "strategy_created_at"),
            "optimization_id": (StrategyOptimization, "optimization_name", "optimization_created_at"),
        },
    )

    bt = session.query(Backtest).one()
    assert bt.strategy_id == local_strategy_id
    assert bt.optimization_id == local_opt_id
```

**Step 2: Run tests to verify they fail**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_sync_receiver.py -v`
Expected: FAIL — `app.services.sync_receiver` doesn't exist (`ModuleNotFoundError`)

**Step 3: Write the implementation**

```python
"""Worker-side receiver for replicated Backtest/StrategyOptimization/Strategy rows.

Matches incoming rows by (name, created_at) — NEVER by the master's raw id, since ids aren't
stable across separate databases (see docs/plans/2026-07-07-remote-result-sync-design.md).
Cross-references (e.g. Backtest.strategy_id) arrive alongside the referenced parent's natural
key, so this module can resolve them to the WORKER's own local id instead of trusting the
master's id verbatim.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Type


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def upsert_by_natural_key(
    session: Any,
    model_cls: Type,
    payload: dict,
    parent_fk: Optional[dict[str, tuple]] = None,
) -> Optional[Any]:
    """Insert or update ``model_cls`` matched by ``(name, created_at)``.

    ``parent_fk`` maps ``{fk_column_name: (parent_model_cls, name_key, created_at_key)}``.
    For each entry, the parent's natural key is popped out of ``payload`` (it isn't a real
    column on ``model_cls``), and the parent is looked up locally by that natural key. If found,
    ``fk_column_name`` is set to the parent's LOCAL id. If not found (parent hasn't synced yet):
    a NULLABLE fk column resolves to None and the row is still written (e.g. a standalone
    Backtest legitimately has no optimization); a NOT NULL fk column (e.g.
    StrategyOptimization.strategy_id) can't take None without violating the schema, so the
    entire write is skipped instead — the caller gets None back, and the next full-state push
    (e.g. the next generation) retries the parent first and self-heals. Never raises.

    Returns the local row (existing, updated in place, or newly inserted), or None if the write
    was skipped because a required parent hasn't synced yet.
    """
    data = dict(payload)
    data.pop("id", None)  # never trust the master's id

    name = data.get("name")
    created_at = _parse_iso(data.get("created_at"))
    data["created_at"] = created_at

    if parent_fk:
        for fk_col, (parent_cls, name_key, created_key) in parent_fk.items():
            parent_name = data.pop(name_key, None)
            parent_created = _parse_iso(data.pop(created_key, None))
            resolved_id = None
            if parent_name is not None and parent_created is not None:
                parent = (
                    session.query(parent_cls)
                    .filter(parent_cls.name == parent_name, parent_cls.created_at == parent_created)
                    .first()
                )
                resolved_id = parent.id if parent else None
            if resolved_id is None and not model_cls.__table__.columns[fk_col].nullable:
                # Required parent not yet synced to this worker — skip rather than violate
                # the NOT NULL constraint. Nothing is lost: the next full-state push retries.
                return None
            data[fk_col] = resolved_id

    existing = (
        session.query(model_cls)
        .filter(model_cls.name == name, model_cls.created_at == created_at)
        .first()
    )
    if existing is not None:
        for key, value in data.items():
            if key in ("name", "created_at"):
                continue
            setattr(existing, key, value)
        session.commit()
        return existing

    row = model_cls(**data)
    session.add(row)
    session.commit()
    return row
```

**Step 4: Run tests to verify they pass**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_sync_receiver.py -v`
Expected: PASS (7 tests)

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/sync_receiver.py \
        testplatform/backend/tests/test_sync_receiver.py
git commit -m "feat(sync): worker-side upsert-by-(name,created_at) with FK remap"
```

---

### Task 3: Worker-side HTTP endpoints

**Files:**
- Modify: `testplatform/backend/app/worker_server.py`
- Test: `testplatform/backend/tests/test_worker_server.py` (append)

**Step 1: Write the failing tests** (append to the existing file, reusing its `client`/`H` fixtures)

```python
def test_sync_strategy_inserts_row(client, monkeypatch, tmp_path):
    db_path = tmp_path / "sync_test.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    import importlib
    import app.models.database as dbmod
    importlib.reload(dbmod)
    monkeypatch.setattr(ws, "SessionLocal", dbmod.SessionLocal, raising=False)

    payload = {"id": 999, "name": "synced-strategy", "created_at": "2026-01-01T00:00:00+00:00"}
    r = client.post("/sync/strategy", headers=H, json=payload)
    assert r.status_code == 200 and r.json()["ok"] is True

    from app.models.strategy import Strategy
    s = dbmod.SessionLocal()
    try:
        rows = s.query(Strategy).all()
        assert len(rows) == 1
        assert rows[0].name == "synced-strategy"
        assert rows[0].id != 999
    finally:
        s.close()


def test_sync_endpoints_require_auth(client):
    assert client.post("/sync/strategy", json={"name": "x", "created_at": "2026-01-01T00:00:00"}).status_code == 401
    assert client.post("/sync/optimization", json={"name": "x", "created_at": "2026-01-01T00:00:00"}).status_code == 401
    assert client.post("/sync/backtest", json={"name": "x", "created_at": "2026-01-01T00:00:00"}).status_code == 401
```

Note on the `db_path`/`reload` dance: `app.models.database` binds `engine`/`SessionLocal` to
`DATABASE_URL` at *import* time (same constraint documented in
`testplatform/backend/tests/conftest.py:27-38`). Since the module is already imported by the time
this test runs, `monkeypatch.setenv` alone won't move its already-bound engine — reloading the
module rebinds it against the new env var, and the endpoint must resolve `SessionLocal` through
`app.worker_server.SessionLocal` (patched here) rather than re-importing it fresh on every
request, so the test can point it at an isolated temp file.

**Step 2: Run tests to verify they fail**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_worker_server.py -k sync -v`
Expected: FAIL — no `/sync/*` routes yet (404)

**Step 3: Add the endpoints to `worker_server.py`**

Add near the top, after the existing imports:

```python
from app.services import cache_sync, self_update, sync_receiver

# Bound lazily on first sync request (see _sync_session()) so tests can monkeypatch it to an
# isolated DB, and so importing this module doesn't eagerly touch app.models.database.
SessionLocal = None
```

Add a small lazy-session helper, right before the `/sync/*` routes:

```python
def _sync_session():
    """Lazily bind SessionLocal to app.models.database's session factory, and ensure the
    strategies/strategy_optimizations/backtests tables exist on whatever engine SessionLocal is
    CURRENTLY bound to.

    Module-level (not per-call) SessionLocal binding so tests can monkeypatch ``ws.SessionLocal``
    to an isolated engine — but the table-existence check runs on every call (like ``/secrets``'s
    ``init_db()``), not just the first: a test can point ``SessionLocal`` at a freshly-created,
    schema-less engine, and that engine needs its tables created too. Table creation goes through
    the model classes' own metadata (``Strategy.metadata``) rather than ``app.models.database.Base``
    directly: ``app.models`` (the package) imports every model at package-import time against ONE
    shared Base, so Strategy/StrategyOptimization/Backtest stay registered there even if
    ``app.models.database`` itself gets reloaded later elsewhere — a reload rebinds that module's
    ``Base``/``engine`` names to fresh objects but does NOT retroactively move already-imported
    model classes onto the new Base, so ``app.models.database.Base.metadata.create_all(...)``
    would silently create zero of the tables we need in that scenario.
    """
    global SessionLocal
    if SessionLocal is None:
        from app.models.database import SessionLocal as _SessionLocal
        SessionLocal = _SessionLocal
    from app.models.strategy import Strategy
    import app.models  # noqa: F401 — registers StrategyOptimization/Backtest alongside Strategy
    session = SessionLocal()
    Strategy.metadata.create_all(bind=session.get_bind())
    return session
```

(Original plan draft only called `init_db()` inside the `if SessionLocal is None:` branch — that
breaks the moment a test monkeypatches `SessionLocal` directly, since the branch never runs and
`init_db()` never fires for that engine. The version above ensures tables exist on every call, and
uses `Strategy.metadata` specifically because a test that reloads `app.models.database` (to point
at an isolated temp DB) gets a fresh `Base`/`engine` that already-imported model classes are NOT
retroactively re-registered on — found and fixed during Task 3's implementation, verified
empirically before the fix.)

Add the three endpoints (after `/secrets`, before `/update`):

```python
@worker_app.post("/sync/strategy")
def sync_strategy(payload: dict, authorization: str = Header(default=None)):
    """Upsert a replicated Strategy row, matched by (name, created_at)."""
    _verify(authorization)
    from app.models.strategy import Strategy
    s = _sync_session()
    try:
        row = sync_receiver.upsert_by_natural_key(s, Strategy, payload)
        return {"ok": True, "skipped": row is None}
    finally:
        s.close()


@worker_app.post("/sync/optimization")
def sync_optimization(payload: dict, authorization: str = Header(default=None)):
    """Upsert a replicated StrategyOptimization row, matched by (name, created_at).

    ``payload`` carries strategy_name/strategy_created_at alongside the master's strategy_id
    so the FK can be remapped to this worker's own local Strategy id (see sync_receiver).
    ``strategy_id`` is NOT NULL on this table, so a missing parent means
    ``upsert_by_natural_key`` skips the write (returns None) rather than raising — reported
    back as ``{"skipped": true}``, still HTTP 200 (expected/benign, self-heals on retry).
    """
    _verify(authorization)
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    s = _sync_session()
    try:
        row = sync_receiver.upsert_by_natural_key(
            s, StrategyOptimization, payload,
            parent_fk={"strategy_id": (Strategy, "strategy_name", "strategy_created_at")},
        )
        return {"ok": True, "skipped": row is None}
    finally:
        s.close()


@worker_app.post("/sync/backtest")
def sync_backtest(payload: dict, authorization: str = Header(default=None)):
    """Upsert a replicated Backtest row, matched by (name, created_at).

    ``payload`` carries both strategy_name/strategy_created_at and
    optimization_name/optimization_created_at so both FKs remap to local ids. Both FK columns
    on this table are nullable, so a missing parent resolves to None and the row is still
    written (unlike StrategyOptimization.strategy_id) — ``skipped`` should always be false here
    in practice, but the return is handled the same way for consistency with the other two
    endpoints.
    """
    _verify(authorization)
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.models.backtest import Backtest
    s = _sync_session()
    try:
        row = sync_receiver.upsert_by_natural_key(
            s, Backtest, payload,
            parent_fk={
                "strategy_id": (Strategy, "strategy_name", "strategy_created_at"),
                "optimization_id": (StrategyOptimization, "optimization_name", "optimization_created_at"),
            },
        )
        return {"ok": True, "skipped": row is None}
    finally:
        s.close()
```

Also update the module docstring's endpoint list (lines 9-17) to add:

```
  POST /sync/strategy    -> {...Strategy row + natural keys} -> upsert by (name, created_at)
  POST /sync/optimization-> {...StrategyOptimization row + strategy natural key} -> upsert
  POST /sync/backtest    -> {...Backtest row + strategy/optimization natural keys} -> upsert
```

**Step 4: Run tests to verify they pass**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_worker_server.py -v`
Expected: PASS (all tests, old and new)

**Step 5: Commit**

```bash
git add testplatform/backend/app/worker_server.py testplatform/backend/tests/test_worker_server.py
git commit -m "feat(sync): add /sync/strategy, /sync/optimization, /sync/backtest endpoints"
```

---

### Task 4: Master-side push client

**Files:**
- Create: `testplatform/backend/app/services/sync_client.py`
- Test: `testplatform/backend/tests/test_sync_client.py`

**Step 1: Write the failing tests**

```python
"""Master-side push client — mocks httpx, no real network or worker process involved."""
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.models import Worker
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization
from app.models.backtest import Backtest
from app.services import sync_client


@pytest.fixture()
def db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'master.sqlite'}")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()
    eng.dispose()


def _seed_worker(db, sync_enabled=True, is_enabled=True):
    w = Worker(
        name="w1", url="http://w1:8100", password="secret", worker_type="remote",
        is_local=False, is_enabled=is_enabled, sync_results_enabled=sync_enabled,
    )
    db.add(w)
    db.commit()
    return w


def test_push_strategy_posts_to_every_enabled_sync_worker(db):
    _seed_worker(db)
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value.raise_for_status.return_value = None

        sync_client.push_strategy(strat, db)

        assert mock_client.post.call_count == 1
        args, kwargs = mock_client.post.call_args
        assert args[0] == "http://w1:8100/sync/strategy"
        assert kwargs["json"]["name"] == "s1"
        assert kwargs["headers"]["Authorization"] == "Bearer secret"


def test_push_skips_workers_with_sync_disabled(db):
    _seed_worker(db, sync_enabled=False)
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()

    with patch("httpx.Client") as mock_client_cls:
        sync_client.push_strategy(strat, db)
        mock_client_cls.assert_not_called()


def test_push_optimization_syncs_parent_strategy_first(db):
    _seed_worker(db)
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()
    opt = StrategyOptimization(
        name="opt1", strategy_id=strat.id, created_at=datetime.now(timezone.utc),
        status="running",
    )
    db.add(opt)
    db.commit()

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value.raise_for_status.return_value = None

        sync_client.push_optimization(opt, db)

        paths = [c.args[0] for c in mock_client.post.call_args_list]
        assert paths == ["http://w1:8100/sync/strategy", "http://w1:8100/sync/optimization"]
        opt_call = mock_client.post.call_args_list[1]
        assert opt_call.kwargs["json"]["strategy_name"] == "s1"


def test_push_backtest_syncs_both_parents(db):
    _seed_worker(db)
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()
    opt = StrategyOptimization(
        name="opt1", strategy_id=strat.id, created_at=datetime.now(timezone.utc), status="completed",
    )
    db.add(opt)
    db.commit()
    bt = Backtest(
        name="TOP1-opt1", engine_type="daily_expert", strategy_id=strat.id, optimization_id=opt.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(bt)
    db.commit()

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value.raise_for_status.return_value = None

        sync_client.push_backtest(bt, db)

        paths = [c.args[0] for c in mock_client.post.call_args_list]
        assert paths[-1] == "http://w1:8100/sync/backtest"
        bt_call = mock_client.post.call_args_list[-1]
        assert bt_call.kwargs["json"]["strategy_name"] == "s1"
        assert bt_call.kwargs["json"]["optimization_name"] == "opt1"


def test_push_failure_is_logged_and_swallowed_not_raised(db):
    _seed_worker(db)
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()

    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.side_effect = ConnectionError("offline")
        sync_client.push_strategy(strat, db)  # must not raise
```

**Step 2: Run tests to verify they fail**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_sync_client.py -v`
Expected: FAIL — `app.services.sync_client` doesn't exist

**Step 3: Write the implementation**

```python
"""Master-side client for replicating Backtest/StrategyOptimization/Strategy rows to every
remote worker with sync enabled — so any worker host can browse/replay past runs even without
the master.

Every push is fire-and-forget PER WORKER: a failure (offline, timeout, auth) is logged and
swallowed, never raised, so an unreachable worker never blocks the optimization/backtest job
it's attached to. Every push sends the row's FULL current state, not a diff, so the next
successful push to a worker that missed one heals the gap automatically — see
docs/plans/2026-07-07-remote-result-sync-design.md.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def _sync_targets(db: Any) -> list[dict]:
    """Every remote, enabled, sync-enabled Worker as a plain dict (mirrors _resolve_workers'
    shape in strategy_optimization_handler.py, deliberately NOT scoped to any one job's
    worker_ids — this is fleet-wide replication, independent of trial-execution routing)."""
    from app.models import Worker
    rows = (
        db.query(Worker)
        .filter(
            Worker.is_local == False,  # noqa: E712
            Worker.is_enabled == True,  # noqa: E712
            Worker.sync_results_enabled == True,  # noqa: E712
        )
        .all()
    )
    return [{"id": w.id, "name": w.name, "url": w.url, "password": w.password} for w in rows]


def _post(worker: dict, path: str, payload: dict, timeout: float = 30.0) -> None:
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(
                f"{str(worker['url']).rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {worker.get('password') or ''}"},
                json=payload,
            )
            r.raise_for_status()
    except Exception as e:  # noqa: BLE001 — fire-and-forget: never block the caller
        logger.warning(f"sync push to worker {worker.get('name')} ({path}) failed: {e!r}")


def _iso(value):
    return value.isoformat() if value is not None else None


def _dump(row: Any) -> dict:
    """Every column on a SQLAlchemy row, JSON-safe (datetimes -> isoformat)."""
    out = {}
    for col in row.__table__.columns:
        value = getattr(row, col.name)
        out[col.name] = _iso(value) if hasattr(value, "isoformat") else value
    return out


def push_strategy(strat: Optional[Any], db: Any) -> None:
    if strat is None:
        return
    payload = _dump(strat)
    for worker in _sync_targets(db):
        _post(worker, "/sync/strategy", payload)


def push_optimization(opt: Optional[Any], db: Any) -> None:
    if opt is None:
        return
    from app.models.strategy import Strategy
    strat = db.query(Strategy).filter(Strategy.id == opt.strategy_id).first() if opt.strategy_id else None
    push_strategy(strat, db)  # dependency first

    payload = _dump(opt)
    payload["strategy_name"] = strat.name if strat else None
    payload["strategy_created_at"] = _iso(strat.created_at) if strat else None
    for worker in _sync_targets(db):
        _post(worker, "/sync/optimization", payload)


def push_backtest(bt: Optional[Any], db: Any) -> None:
    if bt is None:
        return
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization

    strat = db.query(Strategy).filter(Strategy.id == bt.strategy_id).first() if bt.strategy_id else None
    push_strategy(strat, db)

    opt = None
    if bt.optimization_id:
        opt = db.query(StrategyOptimization).filter(StrategyOptimization.id == bt.optimization_id).first()
        push_optimization(opt, db)  # also syncs opt's own Strategy dependency, harmlessly redundant

    payload = _dump(bt)
    payload["strategy_name"] = strat.name if strat else None
    payload["strategy_created_at"] = _iso(strat.created_at) if strat else None
    payload["optimization_name"] = opt.name if opt else None
    payload["optimization_created_at"] = _iso(opt.created_at) if opt else None
    for worker in _sync_targets(db):
        _post(worker, "/sync/backtest", payload)
```

**Step 4: Run tests to verify they pass**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_sync_client.py -v`
Expected: PASS (5 tests)

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/sync_client.py testplatform/backend/tests/test_sync_client.py
git commit -m "feat(sync): master-side push_strategy/push_optimization/push_backtest"
```

---

### Task 5: Wire generation, completion, and failure sync into the GA handler

**Files:**
- Modify: `testplatform/backend/app/services/strategy_optimization_handler.py:170-179` (`_fail`),
  `:376-392` (`ga_callback`), `:588-594` (completion)
- Test: `testplatform/backend/tests/test_strategy_optimization_handler.py` (or wherever the
  existing GA handler tests live — check with `grep -rl ga_callback tests/` first; append there)

**Step 1: Write the failing test**

Find the existing test file exercising `handle_strategy_optimization` end-to-end (it already
seeds a `Strategy` + `StrategyOptimization` and runs a small GA — reuse that setup). Add:

```python
def test_generation_sync_is_called_each_generation(monkeypatch, ...):  # reuse existing fixture args
    """ga_callback must push the optimization row after every generation."""
    from app.services import strategy_optimization_handler as H
    calls = []
    monkeypatch.setattr(H, "push_optimization", lambda opt, db: calls.append(opt.status))

    # ... run a small 2-generation GA the way the existing test does ...

    assert len(calls) >= 2  # at least one push per generation boundary
    assert calls[-1] in ("running", "completed")  # last generation callback, before final completion push


def test_completion_pushes_final_state(monkeypatch, ...):
    from app.services import strategy_optimization_handler as H
    calls = []
    monkeypatch.setattr(H, "push_optimization", lambda opt, db: calls.append(opt.status))

    # ... run the GA to completion ...

    assert calls[-1] == "completed"


def test_failure_path_pushes_failed_status(monkeypatch, ...):
    from app.services import strategy_optimization_handler as H
    calls = []
    monkeypatch.setattr(H, "push_optimization", lambda opt, db: calls.append(opt.status))

    # ... trigger a config-validation failure (e.g. missing fitness_metric) the way an
    # existing test in this file already does, via _fail ...

    assert calls[-1] == "failed"
```

Adapt the `...` placeholders to whatever fixtures/helpers the existing test file already uses to
seed a `Strategy`/`StrategyOptimization` and invoke `handle_strategy_optimization` — do not
invent new scaffolding here; the file already has working examples of both a successful small GA
run and a deliberate failure path (see `_fail` callers).

**Step 2: Run tests to verify they fail**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_strategy_optimization_handler.py -k sync -v`
Expected: FAIL — `push_optimization` isn't imported/called yet, `monkeypatch.setattr` errors with
`AttributeError` (module has no attribute `push_optimization`)

**Step 3: Wire the three call sites**

Add the import near the top of `strategy_optimization_handler.py`, alongside the other
`app.services` imports:

```python
from app.services.sync_client import push_optimization
```

In `_fail` (around line 170-179), push right after the commit:

```python
def _fail(opt_id: int, db: Any, msg: str) -> Dict[str, Any]:
    """Mark the StrategyOptimization row failed + return the failure dict."""
    logger.error(f"strategy_optimization {opt_id} failed: {msg}")
    row = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
    if row:
        row.status = "failed"
        row.error_message = msg[:1000]
        row.completed_at = datetime.now()
        db.commit()
        push_optimization(row, db)
    return {"status": "failed", "error": msg}
```

In `ga_callback` (around line 376-392), push right after the commit:

```python
        def ga_callback(generation: int, best_fitness: float, best_params: Dict):
            pct = ((generation + 1) / int(ga["generations"])) * 100.0
            tq.update_progress(
                task_id,
                pct,
                f"Gen {generation + 1}/{ga['generations']} best={best_fitness:.4f}",
            )
            row = db.query(StrategyOptimization).filter(
                StrategyOptimization.id == opt_id
            ).first()
            row.progress = pct
            row.best_fitness = best_fitness
            row.best_params = best_params
            row.all_results = all_results
            db.commit()
            push_optimization(row, db)
            if tq.is_task_paused(task_id):
                raise InterruptedError("paused/cancelled")
```

At completion (around line 588-594), push right after the commit:

```python
        opt.status = "completed"
        opt.completed_at = datetime.now()
        opt.progress = 100.0
        opt.best_params = result["best_params"]
        opt.best_fitness = result["best_fitness"]
        opt.all_results = all_results
        db.commit()
        push_optimization(opt, db)
        logger.info(
```

**Step 4: Run tests to verify they pass**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_strategy_optimization_handler.py -v`
Expected: PASS (all tests, old and new — the existing GA end-to-end tests must still pass
unmodified, since `push_optimization` fire-and-forget-swallows any error and there's no
`Worker` row seeded in most of them, so `_sync_targets` returns an empty list and the function
is a no-op)

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/strategy_optimization_handler.py \
        testplatform/backend/tests/test_strategy_optimization_handler.py
git commit -m "feat(sync): push StrategyOptimization after each generation, completion, failure"
```

---

### Task 6: Wire top-N backtest sync

**Files:**
- Modify: `testplatform/ba2test_launcher.py:2023-2041` (`_persist_one`)
- Test: find or create a test exercising `_persist_top_backtests`/`_persist_one` — check
  `grep -rl _persist_top_backtests testplatform/backend/tests/` first

**Step 1: Write the failing test**

```python
def test_persist_one_pushes_backtest_after_save(monkeypatch, ...):  # reuse existing fixtures
    import testplatform.ba2test_launcher as launcher  # adjust import path to however the
                                                        # existing tests reach this module
    calls = []
    monkeypatch.setattr(launcher, "push_backtest", lambda bt, db: calls.append(bt.name))

    # ... invoke _persist_top_backtests (or _persist_one directly if it's reachable) the way
    # an existing test in this area already does, with n=1 so exactly one TOP1 row persists ...

    assert calls == ["TOP1-<expected opt name>"]
```

Adapt to the real existing test scaffolding for this function (there should already be a test
seeding a completed `StrategyOptimization` with `all_results` and calling
`_persist_top_backtests` — reuse its fixtures rather than inventing new ones).

**Step 2: Run test to verify it fails**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/ -k persist_one_pushes -v`
Expected: FAIL — `AttributeError: module has no attribute 'push_backtest'`

**Step 3: Wire the call site**

Add the import near the top of `ba2test_launcher.py`, alongside its other `app.services` imports:

```python
from app.services.sync_client import push_backtest
```

In `_persist_one` (around line 2023-2041), push right after the commit, before `return True`:

```python
        def _persist_one(rank, trial_cfg, strategy_params, out) -> bool:
            if not out or not out.get("ok"):
                print(f"    TOP{rank} re-run failed: {(out or {}).get('error', 'no result')}")
                return False
            bt = Backtest(
                name=trial_cfg["name"], model_id=None, engine_type="daily_expert",
                expert_name=expert, optimization_id=opt_id,
                strategy_params=strategy_params,
                start_date=_dt.fromisoformat(str(bt_block["start_date"])),
                end_date=_dt.fromisoformat(str(bt_block["end_date"])),
                initial_capital=float(bt_block["initial_capital"]),
                status="running", started_at=_dt.now(),
            )
            db.add(bt); db.commit(); db.refresh(bt)
            _persist_results(db, bt, out["results"])
            bt.status = "completed"; bt.completed_at = _dt.now()
            bt.is_saved = True  # top performers of a job are kept
            db.commit()
            push_backtest(bt, db)
            return True
```

**Step 4: Run test to verify it passes**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/ -k persist_one_pushes -v`
Expected: PASS

**Step 5: Commit**

```bash
git add testplatform/ba2test_launcher.py testplatform/backend/tests/<the test file you edited>
git commit -m "feat(sync): push top-N backtests to synced workers after persist"
```

---

### Task 7: Wire the `/api/backtests/{id}/save` endpoint

**Files:**
- Modify: `testplatform/backend/app/api/backtests.py:1329-1346`
- Test: `testplatform/backend/tests/test_backtests_api_filters.py` (or the file that already
  tests this router — confirm with `grep -rl 'save_backtest\|/save' tests/`)

**Step 1: Write the failing test**

```python
def test_save_endpoint_pushes_backtest(client, monkeypatch, ...):  # reuse existing client fixture
    from app.api import backtests as backtests_api
    calls = []
    monkeypatch.setattr(backtests_api, "push_backtest", lambda bt, db: calls.append(bt.id))

    bt = _seed_backtest(db, name="unsaved-run", is_saved=False)  # reuse existing seed helper

    r = client.post(f"/api/backtests/{bt.id}/save", json={"name": "kept-run"})

    assert r.status_code == 200
    assert calls == [bt.id]
```

**Step 2: Run test to verify it fails**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_backtests_api_filters.py -k save_endpoint_pushes -v`
Expected: FAIL — `AttributeError: module 'app.api.backtests' has no attribute 'push_backtest'`

**Step 3: Wire the call site**

Add the import near the top of `backtests.py`, alongside its other `app.services` imports:

```python
from app.services.sync_client import push_backtest
```

Modify `save_backtest`:

```python
@router.post("/{backtest_id}/save")
async def save_backtest(
    backtest_id: int,
    save_data: BacktestSave,
    db: Session = Depends(get_db)
):
    """Save a backtest with a custom name (marks it as saved)."""
    backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
    if not backtest:
        raise HTTPException(status_code=404, detail=f"Backtest {backtest_id} not found")

    backtest.name = save_data.name
    backtest.is_saved = True
    db.commit()
    db.refresh(backtest)
    push_backtest(backtest, db)

    logger.info(f"Saved backtest: {backtest.name} (id={backtest_id})")
    return backtest.to_dict()
```

**Step 4: Run test to verify it passes**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_backtests_api_filters.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add testplatform/backend/app/api/backtests.py testplatform/backend/tests/test_backtests_api_filters.py
git commit -m "feat(sync): push backtest to synced workers when saved via the API"
```

---

### Task 8: Wire the CLI `--save` path

**Files:**
- Modify: `testplatform/backend/scripts/run_daily_backtest.py:376-413` (`_persist_tracked`)
- Test: check for an existing test of `_persist_tracked`/`run_daily_backtest.py`'s CLI; if none
  exists, create `testplatform/backend/tests/test_run_daily_backtest_save.py`

**Step 1: Write the failing test**

```python
"""Confirm the --save CLI path pushes a saved Backtest to synced workers, and that --track
without --save does NOT (is_saved stays False -> no sync)."""
import importlib
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_persist_tracked_pushes_when_saved(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli_save.sqlite'}")
    import scripts.run_daily_backtest as script
    importlib.reload(script)

    calls = []
    monkeypatch.setattr(script, "push_backtest", lambda bt, db: calls.append(bt.is_saved))

    config = {
        "name": "cli-saved-run",
        "account_settings": {"commission_per_trade": 0.0, "slippage_bps": 0.0},
        "experts": [{"class": "FMPRating"}],
        "start_date": "2024-01-01", "end_date": "2024-01-31",
        "initial_capital": 10000.0,
    }
    results = {"total_return": 1.0, "total_trades": 0}

    bt_id = script._persist_tracked(config, results, saved=True)

    assert calls == [True]
    assert bt_id is not None


def test_persist_tracked_does_not_push_when_not_saved(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli_track_only.sqlite'}")
    import scripts.run_daily_backtest as script
    importlib.reload(script)

    calls = []
    monkeypatch.setattr(script, "push_backtest", lambda bt, db: calls.append(bt.is_saved))

    config = {
        "name": "cli-tracked-only-run",
        "account_settings": {"commission_per_trade": 0.0, "slippage_bps": 0.0},
        "experts": [{"class": "FMPRating"}],
        "start_date": "2024-01-01", "end_date": "2024-01-31",
        "initial_capital": 10000.0,
    }
    results = {"total_return": 1.0, "total_trades": 0}

    script._persist_tracked(config, results, saved=False)

    assert calls == []
```

**Step 2: Run tests to verify they fail**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_run_daily_backtest_save.py -v`
Expected: FAIL — `AttributeError: module 'scripts.run_daily_backtest' has no attribute 'push_backtest'`

**Step 3: Wire the call site**

Add the import near the top of `run_daily_backtest.py`, alongside its other imports:

```python
from app.services.sync_client import push_backtest
```

Modify `_persist_tracked` (around line 380-413):

```python
    db = SessionLocal()
    try:
        bt = Backtest(
            name=config["name"],
            model_id=None,  # daily expert runs are not model-driven
            engine_type="daily_expert",
            expert_name=expert_name,
            start_date=config["start_date"],
            end_date=config["end_date"],
            initial_capital=float(config["initial_capital"]),
            commission=float(acct["commission_per_trade"]),
            slippage=float(acct["slippage_bps"]),
            status="running",
            started_at=datetime.now(),
        )
        db.add(bt)
        db.commit()
        db.refresh(bt)
        _persist_results(db, bt, results)
        bt.status = "completed"
        bt.completed_at = datetime.now()
        bt.is_saved = bool(saved)
        db.commit()
        if bt.is_saved:
            push_backtest(bt, db)
        return bt.id
    finally:
        db.close()
```

**Step 4: Run tests to verify they pass**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_run_daily_backtest_save.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add testplatform/backend/scripts/run_daily_backtest.py testplatform/backend/tests/test_run_daily_backtest_save.py
git commit -m "feat(sync): push backtest to synced workers when --save is passed"
```

---

### Task 9: Crash-recovery status fix + push on restart

**Files:**
- Modify: `testplatform/backend/app/main.py:354-381` (`recover_interrupted_jobs`)
- Test: `testplatform/backend/tests/test_recover_interrupted_jobs.py`

**Step 1: Write the failing tests**

```python
"""Startup crash recovery: a StrategyOptimization stuck at status='running' after a crash
must be corrected to 'failed' and pushed to synced workers — today only its TaskQueue row
gets fixed (marked 'stopped'), the StrategyOptimization itself is left lying about its state."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.models.task_queue import TaskQueue, TaskStatus
from app.models.strategy_optimization import StrategyOptimization


@pytest.fixture()
def db_session_factory(tmp_path, monkeypatch):
    db_path = tmp_path / "recover_test.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    yield Session
    eng.dispose()


def test_crashed_optimization_status_is_corrected_and_pushed(db_session_factory, monkeypatch):
    s = db_session_factory()
    opt = StrategyOptimization(name="crashed-opt", status="running", created_at=datetime.now(timezone.utc))
    s.add(opt)
    s.commit()
    task = TaskQueue(
        task_id="t-1", task_type="strategy_optimization", name="crashed-opt-task",
        status=TaskStatus.RUNNING.value, payload={"optimization_id": opt.id},
    )
    s.add(task)
    s.commit()
    s.close()

    with patch("app.models.database.SessionLocal", db_session_factory):
        pushed = []
        with patch("app.main.push_optimization", lambda o, db: pushed.append(o.status)):
            from app.main import recover_interrupted_jobs
            recover_interrupted_jobs()

    s2 = db_session_factory()
    try:
        refreshed = s2.query(StrategyOptimization).filter(StrategyOptimization.id == opt.id).first()
        assert refreshed.status == "failed"
        assert "restart" in (refreshed.error_message or "").lower()
        assert pushed == ["failed"]

        refreshed_task = s2.query(TaskQueue).filter(TaskQueue.task_id == "t-1").first()
        assert refreshed_task.status == TaskStatus.STOPPED.value  # existing behaviour untouched
    finally:
        s2.close()


def test_non_optimization_task_types_are_left_alone(db_session_factory, monkeypatch):
    s = db_session_factory()
    task = TaskQueue(
        task_id="t-2", task_type="training_job", name="unrelated-crashed-job",
        status=TaskStatus.RUNNING.value, payload={"some_other_key": 123},
    )
    s.add(task)
    s.commit()
    s.close()

    with patch("app.models.database.SessionLocal", db_session_factory):
        with patch("app.main.push_optimization") as mock_push:
            from app.main import recover_interrupted_jobs
            recover_interrupted_jobs()
            mock_push.assert_not_called()


def test_completed_optimization_is_not_touched(db_session_factory, monkeypatch):
    """A crashed TaskQueue row pointing at an ALREADY-completed optimization (e.g. the crash
    happened during a later, unrelated step) must not be clobbered back to 'failed'."""
    s = db_session_factory()
    opt = StrategyOptimization(name="already-done", status="completed", created_at=datetime.now(timezone.utc))
    s.add(opt)
    s.commit()
    task = TaskQueue(
        task_id="t-3", task_type="strategy_optimization", name="already-done-task",
        status=TaskStatus.RUNNING.value, payload={"optimization_id": opt.id},
    )
    s.add(task)
    s.commit()
    s.close()

    with patch("app.models.database.SessionLocal", db_session_factory):
        with patch("app.main.push_optimization") as mock_push:
            from app.main import recover_interrupted_jobs
            recover_interrupted_jobs()
            mock_push.assert_not_called()

    s2 = db_session_factory()
    try:
        refreshed = s2.query(StrategyOptimization).filter(StrategyOptimization.id == opt.id).first()
        assert refreshed.status == "completed"
    finally:
        s2.close()
```

**Step 2: Run tests to verify they fail**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_recover_interrupted_jobs.py -v`
Expected: FAIL — `AttributeError: module 'app.main' has no attribute 'push_optimization'` (not
imported there yet), and the status-correction behavior doesn't exist yet

**Step 3: Extend `recover_interrupted_jobs()`**

Add the import near the top of `main.py`, alongside its other `app.services` imports:

```python
from app.services.sync_client import push_optimization
```

Replace `recover_interrupted_jobs()`:

```python
def recover_interrupted_jobs():
    """Mark running jobs as stopped on startup (they crashed and can be resumed)."""
    from app.models.database import SessionLocal
    from app.models.task_queue import TaskQueue, TaskStatus

    db = SessionLocal()
    try:
        # Find jobs that were running when the app crashed
        running_jobs = db.query(TaskQueue).filter(
            TaskQueue.status == TaskStatus.RUNNING.value
        ).all()

        for job in running_jobs:
            logger.warning(f"Found interrupted job {job.task_id}, marking as stopped")
            job.status = TaskStatus.STOPPED.value
            job.progress_message = "Interrupted - can be resumed"

        if running_jobs:
            db.commit()
            logger.info(f"Recovered {len(running_jobs)} interrupted jobs - marked as 'stopped'")
        else:
            logger.info("No interrupted jobs found")

        _recover_interrupted_optimizations(db, running_jobs)

    except Exception as e:
        logger.error(f"Failed to recover interrupted jobs: {e}")
        db.rollback()
    finally:
        db.close()


def _recover_interrupted_optimizations(db, crashed_task_rows) -> None:
    """Correct StrategyOptimization.status for crashed 'strategy_optimization' TaskQueue rows.

    The TaskQueue-level recovery above only fixes the task row itself; it never touches the
    PARENT StrategyOptimization, which is left reporting status='running' forever after a
    crash — both locally and, before this fix, forever on any synced remote copy too. Only
    rows genuinely still 'running' are touched, so an optimization that had already reached a
    terminal state before the crash (e.g. the crash happened during an unrelated later step)
    is left alone.
    """
    from app.models.strategy_optimization import StrategyOptimization

    for job in crashed_task_rows:
        if job.task_type != "strategy_optimization":
            continue
        opt_id = (job.payload or {}).get("optimization_id")
        if not opt_id:
            continue
        opt = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
        if opt is None or opt.status != "running":
            continue
        opt.status = "failed"
        opt.error_message = "Interrupted by server restart"
        db.commit()
        logger.warning(
            f"StrategyOptimization {opt_id} was stuck 'running' after a crash — marked 'failed'"
        )
        push_optimization(opt, db)
```

**Step 4: Run tests to verify they pass**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_recover_interrupted_jobs.py -v`
Expected: PASS (3 tests)

**Step 5: Run the full existing `main.py`-adjacent test suite to confirm nothing else broke**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/ -k "startup or recover" -v`
Expected: PASS

**Step 6: Commit**

```bash
git add testplatform/backend/app/main.py testplatform/backend/tests/test_recover_interrupted_jobs.py
git commit -m "fix(optimize): correct StrategyOptimization.status on restart, sync the fix"
```

---

### Task 10: Manual backfill script

**Files:**
- Create: `testplatform/backend/scripts/sync_backfill.py`
- Test: `testplatform/backend/tests/test_sync_backfill.py`

**Step 1: Write the failing test**

```python
"""Manual backfill script: pushes pre-existing is_saved backtests and terminal-status
optimizations to synced workers. Run on demand, never automatically."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization
from app.models.backtest import Backtest


@pytest.fixture()
def db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'backfill.sqlite'}")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()
    eng.dispose()


def test_backfill_pushes_saved_backtests_and_terminal_optimizations(db):
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()

    completed_opt = StrategyOptimization(
        name="done-opt", strategy_id=strat.id, status="completed", created_at=datetime.now(timezone.utc)
    )
    running_opt = StrategyOptimization(  # must NOT be pushed — not terminal
        name="still-running-opt", strategy_id=strat.id, status="running", created_at=datetime.now(timezone.utc)
    )
    db.add_all([completed_opt, running_opt])
    db.commit()

    saved_bt = Backtest(
        name="saved-run", engine_type="daily_expert", strategy_id=strat.id,
        is_saved=True, created_at=datetime.now(timezone.utc),
    )
    unsaved_bt = Backtest(  # must NOT be pushed
        name="scratch-run", engine_type="daily_expert", strategy_id=strat.id,
        is_saved=False, created_at=datetime.now(timezone.utc),
    )
    db.add_all([saved_bt, unsaved_bt])
    db.commit()

    from scripts import sync_backfill

    with patch.object(sync_backfill, "push_optimization") as mock_push_opt, \
         patch.object(sync_backfill, "push_backtest") as mock_push_bt:
        sync_backfill.run(db)

        pushed_opt_names = [c.args[0].name for c in mock_push_opt.call_args_list]
        assert pushed_opt_names == ["done-opt"]

        pushed_bt_names = [c.args[0].name for c in mock_push_bt.call_args_list]
        assert pushed_bt_names == ["saved-run"]
```

**Step 2: Run test to verify it fails**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_sync_backfill.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.sync_backfill`

**Step 3: Write the script**

```python
#!/usr/bin/env python3
"""Manual, one-off backfill: push pre-existing saved backtests and terminal optimizations to
every synced worker.

Sync (Task 5-8 of docs/plans/2026-07-07-remote-result-sync-implementation.md) only fires for
NEW rows going forward. Rows that already existed before this feature shipped are never
backfilled automatically (see the design doc's "Migration and Rollout" section) — run this
script once, on demand, if you want history replicated too.

Usage:
    ./venv/bin/python scripts/sync_backfill.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(db) -> None:
    from app.models.strategy_optimization import StrategyOptimization
    from app.models.backtest import Backtest
    from app.services.sync_client import push_optimization, push_backtest

    terminal_opts = (
        db.query(StrategyOptimization)
        .filter(StrategyOptimization.status.in_(["completed", "failed"]))
        .all()
    )
    print(f"Backfilling {len(terminal_opts)} terminal-status optimization(s)...")
    for opt in terminal_opts:
        push_optimization(opt, db)
        print(f"  pushed optimization: {opt.name} (id={opt.id})")

    saved_backtests = db.query(Backtest).filter(Backtest.is_saved == True).all()  # noqa: E712
    print(f"Backfilling {len(saved_backtests)} saved backtest(s)...")
    for bt in saved_backtests:
        push_backtest(bt, db)
        print(f"  pushed backtest: {bt.name} (id={bt.id})")


def main() -> int:
    from app.models.database import SessionLocal

    db = SessionLocal()
    try:
        run(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Step 4: Run test to verify it passes**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_sync_backfill.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add testplatform/backend/scripts/sync_backfill.py testplatform/backend/tests/test_sync_backfill.py
git commit -m "feat(sync): add manual sync_backfill.py script for pre-existing rows"
```

---

### Task 11: End-to-end integration test against a real worker_server.py

**Files:**
- Create: `testplatform/backend/tests/test_sync_integration.py`

**Step 1: Write the test**

This drives the full chain — a real (in-process, via TestClient) `worker_server.py` — through
`sync_client.push_backtest`, then re-opens the worker's DB file directly to prove the rows are
there and correctly cross-referenced, exactly as a full backend on that host would see them.

```python
"""End-to-end: master pushes a Strategy -> StrategyOptimization -> Backtest chain to a real (in
process) worker_server.py instance, then a SEPARATE session against that same DB file confirms
the rows are queryable with correctly remapped local ids — proving Section on cross-reference
resolution in docs/plans/2026-07-07-remote-result-sync-design.md end to end."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

import app.worker_server as ws
from app.models.database import Base
from app.models import Worker
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization
from app.models.backtest import Backtest
from app.services import sync_client


@pytest.fixture()
def worker_db_path(tmp_path):
    return tmp_path / "worker_side.sqlite"


@pytest.fixture()
def worker_client(monkeypatch, worker_db_path):
    monkeypatch.setattr(ws, "_PASSWORD", "secret")
    monkeypatch.setattr(ws, "_CAPACITY", 1)
    monkeypatch.setattr(ws, "_POOL", object())  # unused by /sync/* endpoints

    eng = create_engine(f"sqlite:///{worker_db_path}")
    Session = sessionmaker(bind=eng)
    monkeypatch.setattr(ws, "SessionLocal", Session)
    return TestClient(ws.worker_app)


@pytest.fixture()
def master_db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'master_side.sqlite'}")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()
    eng.dispose()


def _fake_worker_url_client(worker_client):
    """Patch httpx.Client so sync_client's POSTs are routed into the in-process TestClient
    instead of a real socket."""
    class _Adapter:
        def __init__(self, tc):
            self._tc = tc

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            path = url.split("://", 1)[1].split("/", 1)[1]
            return self._tc.post(f"/{path}", headers=headers, json=json)

    return _Adapter(worker_client)


def test_full_chain_replicates_with_correct_local_ids(master_db, worker_client, worker_db_path):
    w = Worker(
        name="w1", url="http://fake-worker", password="secret", worker_type="remote",
        is_local=False, is_enabled=True, sync_results_enabled=True,
    )
    master_db.add(w)
    master_db.commit()

    strat = Strategy(name="e2e-strat", created_at=datetime.now(timezone.utc))
    master_db.add(strat)
    master_db.commit()
    opt = StrategyOptimization(
        name="e2e-opt", strategy_id=strat.id, status="completed",
        created_at=datetime.now(timezone.utc),
    )
    master_db.add(opt)
    master_db.commit()
    bt = Backtest(
        name="TOP1-e2e-opt", engine_type="daily_expert", strategy_id=strat.id,
        optimization_id=opt.id, is_saved=True, created_at=datetime.now(timezone.utc),
    )
    master_db.add(bt)
    master_db.commit()

    with patch("httpx.Client", return_value=_fake_worker_url_client(worker_client)):
        sync_client.push_backtest(bt, master_db)

    # Open a SEPARATE session against the worker's file, as a full backend would on that host.
    verify_eng = create_engine(f"sqlite:///{worker_db_path}")
    VerifySession = sessionmaker(bind=verify_eng)
    vs = VerifySession()
    try:
        w_strat = vs.query(Strategy).filter(Strategy.name == "e2e-strat").one()
        w_opt = vs.query(StrategyOptimization).filter(StrategyOptimization.name == "e2e-opt").one()
        w_bt = vs.query(Backtest).filter(Backtest.name == "TOP1-e2e-opt").one()

        assert w_opt.strategy_id == w_strat.id
        assert w_bt.strategy_id == w_strat.id
        assert w_bt.optimization_id == w_opt.id
        assert w_strat.id != strat.id  # proves it's a genuinely local id, not the master's
    finally:
        vs.close()
        verify_eng.dispose()
```

**Step 2: Run the test**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/test_sync_integration.py -v`
Expected: PASS. If it fails, the likely culprit is the URL-splitting in `_fake_worker_url_client`
or a stale `_sync_session()` caching a `SessionLocal` from an earlier test — check that fixture's
`monkeypatch.setattr(ws, "SessionLocal", Session)` actually takes effect before the first request
(it must run before `_sync_session()`'s `if SessionLocal is None` check ever sees a real value).

**Step 3: Commit**

```bash
git add testplatform/backend/tests/test_sync_integration.py
git commit -m "test(sync): end-to-end integration test through a real worker_server.py"
```

---

### Task 12: Full test suite + live smoke test

**Step 1: Run the entire backend test suite**

Run: `cd testplatform/backend && ./venv/bin/python -m pytest tests/ -q`
Expected: all pass, including everything untouched by this feature (confirms no regressions in
`strategy_optimization_handler.py`, `ba2test_launcher.py`, `backtests.py`, `main.py`).

**Step 2: Live smoke test (manual, not automated)**

This needs a real remote worker reachable over HTTP — follow
`docs/plans/2026-07-07-remote-result-sync-design.md`'s Testing Plan section:

1. Start a real worker: `ba2-test worker --port 8100 --password <secret>` on a second host (or a
   second port on the same machine).
2. Register it as a `Worker` row with `sync_results_enabled=True` (default) via the workers API/UI.
3. Kick off a small real optimization job (`ba2-test optimize ...` or the UI) with a handful of
   generations.
4. After each generation, check the worker's `dl_forecasting.db` (`TEST_DIR/dl_forecasting.db`
   on that host) for a `strategy_optimizations` row matching by name, with `progress`/
   `best_fitness`/`all_results` advancing.
5. Let it finish; confirm the top-N `backtests` rows appear on the worker with `is_saved=1` and
   correctly resolved `optimization_id`/`strategy_id` (join back to the worker's own
   `strategy_optimizations`/`strategies` tables to confirm no NULLs).
6. Start the full backend (`./venv/bin/python -m uvicorn app.main:app`) ON THAT SAME worker host
   and confirm the synced optimization/backtests show up in its normal UI immediately.
7. Kill the master process mid-run of a fresh optimization job (`kill -9` or Task Manager, not a
   graceful shutdown). Restart it. Confirm: (a) the master's own `StrategyOptimization.status`
   flips to `'failed'` with `error_message` mentioning "restart", (b) the worker's copy of that
   same row also flips to `'failed'` shortly after restart.

Document the outcome (pass/fail, screenshots or query output) in the PR description — this step
has no automated assertion, it's the final human check that the whole system works against a
real network hop, real process crash, and real second SQLite file.
