"""The startup housekeeping pass: purge old ``activitylog`` rows, then VACUUM if — and
only if — the freelist is worth reclaiming.

WHY THIS EXISTS. On the user's live database ``activitylog`` had reached 27,640 rows /
66.9 MB, 88.4% of it older than 60 days. Nothing ever deleted it except a manual button
in Settings that nobody presses.

THE THREE PROPERTIES THAT MATTER, and why each has a test rather than a comment:

1. THE ``WHERE`` CLAUSE. A purge with an inverted comparison deletes the recent rows and
   keeps the old ones; a purge with no ``WHERE`` at all empties the table; a purge that
   names the wrong table empties a different one. All three "pass" a test that only
   counts what is gone, so the tests below assert on WHICH rows survive, on the exact
   boundary, and on the row counts of the NEIGHBOURING tables.

2. THE VACUUM GATE. ``VACUUM`` rewrites the entire file and holds an EXCLUSIVE lock on
   the whole database while it does. Unconditionally rewriting 399 MB on every boot to
   reclaim nothing is a bad trade, so it is gated on the freelist. "Always vacuums" and
   "never vacuums" each pass a one-sided test, so both directions are asserted.

3. NOTHING HERE MAY BLOCK STARTUP. A failed purge or a failed vacuum must log loudly and
   let the platform boot: housekeeping does not get to stop trading. Equally it must not
   fail silently — that is how the table reached 66.9 MB.

HOW IT RUNS.
  * TIME IS FROZEN and deliberately NOT today (2024-11-05). This is date-window logic; a
    test frozen to the system clock passes for the wrong reason.
  * THE DATABASE IS A THROWAWAY FILE under ``tmp_path``. It must be a real file, not the
    conftest's ``:memory:`` engine: ``PRAGMA freelist_count``, ``VACUUM`` and the file
    size are the whole subject. The real databases are never opened.
  * NEVER ``caplog`` — ``logger.py`` sets ``propagate = False``. ``_capture_errors``
    patches the module-under-test's own logger.
"""
from __future__ import annotations

import ast
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, select, func

from ba2_common.core.models import (
    ActivityLog, AnalysisOutput, MarketAnalysis, TradeActionResult,
)
from ba2_common.core.types import (
    ActivityLogSeverity, ActivityLogType, MarketAnalysisStatus,
)


REPO_ROOT = Path(__file__).resolve().parents[1]

# Frozen, and years away from the system clock on purpose.
NOW = datetime(2024, 11, 5, 9, 30, 0, tzinfo=timezone.utc)

RETENTION = 60


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """A REAL sqlite file, built with the production engine settings, wired in as the
    package engine for the duration of the test.

    The conftest's autouse ``patch_db_engine`` points ``ba2_common.core.db._engine`` at a
    shared ``:memory:`` engine. That engine has no file, no freelist worth measuring and
    cannot be vacuumed meaningfully, so every test here overrides it with a file engine
    built by the production ``_build_engine`` (same WAL/busy-timeout pragmas as live).
    """
    import ba2_common.core.db as pkg_db

    path = tmp_path / "maintenance.sqlite"
    engine = pkg_db._build_engine(str(path))
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(pkg_db, "_engine", engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _capture_errors(monkeypatch, module):
    """Collect ``logger.error`` messages from *module*. NOT caplog."""
    messages = []
    monkeypatch.setattr(module.logger, "error", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def _add_logs(engine, ages_in_days, *, payload_bytes=0):
    """Insert one ActivityLog per age (days before ``NOW``). Returns their descriptions."""
    names = []
    with Session(engine) as session:
        for age in ages_in_days:
            name = f"age={age}"
            session.add(ActivityLog(
                created_at=NOW - timedelta(days=age),
                severity=ActivityLogSeverity.INFO,
                type=ActivityLogType.APPLICATION_STATUS_CHANGE,
                description=name,
                data={"pad": "x" * payload_bytes} if payload_bytes else {},
            ))
            names.append(name)
        session.commit()
    return names


def _log_descriptions(engine):
    with Session(engine) as session:
        return sorted(session.exec(select(ActivityLog.description)).all())


def _count(engine, model):
    with Session(engine) as session:
        return session.exec(select(func.count()).select_from(model)).one()


def _pragma(engine, name):
    with engine.connect() as conn:
        return conn.exec_driver_sql(f"PRAGMA {name}").scalar()


def _free_bytes(engine):
    return _pragma(engine, "freelist_count") * _pragma(engine, "page_size")


def _db_path(engine):
    return engine.url.database


def _db_size(engine):
    """Size of the database ON DISK, with the WAL folded in first.

    In WAL mode the main file lags reality by everything not yet checkpointed — a fresh
    2.9 MB test database measures 4 kB — so a naive ``getsize`` would make any before/
    after comparison meaningless (and did, until this helper existed).
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    return os.path.getsize(_db_path(engine))


def _purge(days=RETENTION, now=NOW):
    """The single purge implementation — the one the Settings tab calls."""
    from ba2_trade_platform.core.cleanup import cleanup_activity_logs
    return cleanup_activity_logs(days_to_keep=days, now=now)


# ---------------------------------------------------------------------------
# WHICH ROWS GO
# ---------------------------------------------------------------------------

def test_rows_older_than_the_window_are_deleted_and_newer_ones_are_kept(file_db):
    _add_logs(file_db, [400, 200, 61, 59, 30, 0])

    result = _purge()

    assert result["error"] is None
    assert result["deleted_count"] == 3
    assert _log_descriptions(file_db) == ["age=0", "age=30", "age=59"]


def test_a_row_exactly_on_the_boundary_is_kept(file_db):
    """EXACTLY 60 days old is KEPT.

    "Keep 60 days" is a closed window: a row whose age is exactly the retention period is
    still inside it. The comparison is therefore ``created_at < now - 60d``, strictly.
    The microsecond either side of that line is pinned by the two tests around this one,
    because an off-by-one here is invisible in any test that only counts rows.
    """
    _add_logs(file_db, [60])

    result = _purge()

    assert result["deleted_count"] == 0
    assert _log_descriptions(file_db) == ["age=60"]


def test_one_microsecond_older_than_the_boundary_is_purged(file_db):
    with Session(file_db) as session:
        session.add(ActivityLog(
            created_at=NOW - timedelta(days=60, microseconds=1),
            severity=ActivityLogSeverity.INFO,
            type=ActivityLogType.APPLICATION_STATUS_CHANGE,
            description="a microsecond too old", data={}))
        session.add(ActivityLog(
            created_at=NOW - timedelta(days=60) + timedelta(microseconds=1),
            severity=ActivityLogSeverity.INFO,
            type=ActivityLogType.APPLICATION_STATUS_CHANGE,
            description="a microsecond too young", data={}))
        session.commit()

    result = _purge()

    assert result["deleted_count"] == 1
    assert _log_descriptions(file_db) == ["a microsecond too young"]


def test_an_empty_table_is_not_an_error(file_db):
    result = _purge()
    assert result == {"deleted_count": 0, "error": None}


def test_nothing_else_is_deleted(file_db):
    """The failure mode that matters: a ``DELETE`` aimed at the wrong table.

    Every neighbouring table is seeded with rows OLDER than the retention window, so a
    purge that forgot which table it was purging — or dropped its ``WHERE`` — takes them
    with it and this test says so.
    """
    _add_logs(file_db, [400, 0])
    old = NOW - timedelta(days=400)
    with Session(file_db) as session:
        analysis = MarketAnalysis(created_at=old, symbol="AAPL", expert_instance_id=1,
                                  status=MarketAnalysisStatus.COMPLETED)
        session.add(analysis)
        session.commit()
        session.refresh(analysis)
        session.add(AnalysisOutput(market_analysis_id=analysis.id, name="AAPL_report",
                                   type="report", text="old report", created_at=old))
        session.add(TradeActionResult(created_at=old, action_type="OPEN", success=True,
                                      message="opened", expert_recommendation_id=1,
                                      data={"pad": "x" * 100}))
        session.commit()

    before = {m: _count(file_db, m) for m in (MarketAnalysis, AnalysisOutput, TradeActionResult)}
    assert all(v == 1 for v in before.values()), before

    _purge()

    after = {m: _count(file_db, m) for m in (MarketAnalysis, AnalysisOutput, TradeActionResult)}
    assert after == before
    assert _count(file_db, ActivityLog) == 1


def test_the_purge_does_not_load_the_rows_it_deletes(file_db):
    """One bulk ``DELETE``; no ``SELECT`` of the rows.

    The original implementation materialised every matching row as an ORM object and
    called ``session.delete()`` on each — 24,435 objects on the user's database. Fine
    behind a manual button, wrong on the startup path. The statement log is asserted
    directly because "it feels faster" is not a regression test.
    """
    from sqlalchemy import event

    _add_logs(file_db, [400, 300, 0])
    statements = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(file_db, "before_cursor_execute", _record)
    try:
        _purge()
    finally:
        event.remove(file_db, "before_cursor_execute", _record)

    touching = [s for s in statements if "activitylog" in s.lower()]
    assert len(touching) == 1, touching
    assert touching[0].strip().upper().startswith("DELETE"), touching[0]
    # ...and a PLAIN delete. ``synchronize_session='fetch'`` also emits exactly one
    # statement on modern SQLite — ``DELETE ... RETURNING id`` — which streams 24,435
    # primary keys back to Python for a session that holds none of those objects.
    assert "RETURNING" not in touching[0].upper(), touching[0]


# ---------------------------------------------------------------------------
# RETENTION IS CONFIGURABLE
# ---------------------------------------------------------------------------

def test_retention_is_configurable(file_db):
    _add_logs(file_db, [45])

    kept = _purge(days=60)
    assert kept["deleted_count"] == 0

    purged = _purge(days=30)
    assert purged["deleted_count"] == 1
    assert _log_descriptions(file_db) == []


def test_retention_defaults_to_sixty_days():
    from ba2_common.core.db_maintenance import (
        DEFAULT_ACTIVITY_LOG_RETENTION_DAYS, resolve_retention_days,
    )
    assert DEFAULT_ACTIVITY_LOG_RETENTION_DAYS == 60
    assert resolve_retention_days() == 60


def test_retention_can_be_set_from_the_environment(monkeypatch):
    from ba2_common.core.db_maintenance import (
        ACTIVITY_LOG_RETENTION_DAYS_ENV, resolve_retention_days,
    )
    monkeypatch.setenv(ACTIVITY_LOG_RETENTION_DAYS_ENV, "180")
    assert resolve_retention_days() == 180
    # An explicit argument still wins over the environment.
    assert resolve_retention_days(30) == 30


@pytest.mark.parametrize("bad", ["", "  ", "abc", "0", "-5", "60.5", "sixty", "1e3", None])
def test_a_bad_retention_value_is_refused_not_silently_defaulted(monkeypatch, bad):
    """A misconfigured retention must be REFUSED.

    Falling back to 60 on a typo means the operator who set 180 quietly gets 60 and
    deletes four months of history they meant to keep. ``None`` is in this list as the
    ENVIRONMENT value ``"None"`` — a real thing people write into .env files.
    """
    from ba2_common.core.db_maintenance import (
        ACTIVITY_LOG_RETENTION_DAYS_ENV, resolve_retention_days,
    )
    monkeypatch.setenv(ACTIVITY_LOG_RETENTION_DAYS_ENV, "None" if bad is None else bad)
    with pytest.raises(ValueError) as excinfo:
        resolve_retention_days()
    assert ACTIVITY_LOG_RETENTION_DAYS_ENV in str(excinfo.value)


@pytest.mark.parametrize("bad", ["abc", 0, -1, 60.5, True])
def test_a_bad_retention_argument_is_refused(bad):
    from ba2_common.core.db_maintenance import resolve_retention_days
    with pytest.raises(ValueError):
        resolve_retention_days(bad)


# ---------------------------------------------------------------------------
# THE VACUUM GATE
# ---------------------------------------------------------------------------

def _make_freelist(engine, *, rows=60, payload_bytes=40_000):
    """Delete a pile of fat rows so the file has a real freelist to reclaim."""
    _add_logs(engine, [400] * rows, payload_bytes=payload_bytes)
    _purge()
    free = _free_bytes(engine)
    assert free > 0, "the fixture failed to produce a freelist"
    return free


def test_vacuum_runs_when_the_freelist_is_above_the_threshold(file_db):
    from ba2_common.core.db_maintenance import vacuum_if_needed

    free = _make_freelist(file_db)
    size_before = _db_size(file_db)

    result = vacuum_if_needed(min_free_mb=(free / 1_000_000) / 2)

    assert result["vacuumed"] is True
    # BEFORE anything else checkpoints for us: in WAL mode ``VACUUM`` truncates the main
    # file but leaves a -wal sidecar the size of the WHOLE database (measured: a 399 MB
    # database left a 401.8 MB -wal). Without the checkpoint the vacuum "reclaims"
    # 103 MB and costs 400 MB of disk until something else folds the WAL back.
    wal = _db_path(file_db) + "-wal"
    wal_left = os.path.getsize(wal) if os.path.exists(wal) else 0
    assert wal_left == 0, f"the vacuumed image was left in the -wal sidecar ({wal_left} bytes)"

    assert _free_bytes(file_db) == 0
    assert _db_size(file_db) < size_before
    assert result["reclaimed_bytes"] > 0


def test_vacuum_is_skipped_when_the_freelist_is_below_the_threshold(file_db):
    """The expensive half of the gate. ``VACUUM`` takes an EXCLUSIVE lock on the whole
    database and rewrites every page; doing that on every boot to reclaim a few KB is
    the trade this threshold exists to refuse."""
    from ba2_common.core.db_maintenance import vacuum_if_needed

    free = _make_freelist(file_db)
    size_before = _db_size(file_db)

    result = vacuum_if_needed(min_free_mb=(free / 1_000_000) * 10)

    assert result["vacuumed"] is False
    assert _free_bytes(file_db) == free, "a skipped vacuum must not touch the freelist"
    assert _db_size(file_db) == size_before


def test_the_vacuum_threshold_boundary_is_inclusive(file_db):
    """Exactly at the threshold counts as "worth it"."""
    from ba2_common.core.db_maintenance import vacuum_if_needed

    free = _make_freelist(file_db)
    result = vacuum_if_needed(min_free_mb=free / 1_000_000)
    assert result["vacuumed"] is True


def test_the_vacuum_threshold_defaults_and_is_configurable(monkeypatch):
    from ba2_common.core.db_maintenance import (
        DEFAULT_VACUUM_MIN_FREE_MB, VACUUM_MIN_FREE_MB_ENV, resolve_vacuum_min_free_mb,
    )
    assert resolve_vacuum_min_free_mb() == DEFAULT_VACUUM_MIN_FREE_MB
    monkeypatch.setenv(VACUUM_MIN_FREE_MB_ENV, "250")
    assert resolve_vacuum_min_free_mb() == 250.0
    assert resolve_vacuum_min_free_mb(1.5) == 1.5


@pytest.mark.parametrize("bad", ["abc", "-1", "", "None"])
def test_a_bad_vacuum_threshold_is_refused(monkeypatch, bad):
    from ba2_common.core.db_maintenance import (
        VACUUM_MIN_FREE_MB_ENV, resolve_vacuum_min_free_mb,
    )
    monkeypatch.setenv(VACUUM_MIN_FREE_MB_ENV, bad)
    with pytest.raises(ValueError):
        resolve_vacuum_min_free_mb()


def test_the_reclaimed_figure_is_not_computed_from_an_unflushed_file(file_db):
    """Measure the database, not the tip of the iceberg.

    Deliberately does NOT checkpoint before calling: in a live process the WAL is dirty
    when the app starts, and in WAL mode the main file can be a tiny fraction of the real
    database (measured here: 4 kB against 2.9 MB). Sizing the "before" off that file
    reports a NEGATIVE reclaim — the first version of this code logged
    "reclaimed -450.6 kB" — and every other test in this file inadvertently hides it,
    because their own size helper checkpoints first.
    """
    from ba2_common.core.db_maintenance import vacuum_if_needed

    _make_freelist(file_db)  # leaves several MB sitting in the -wal, as live would

    result = vacuum_if_needed(min_free_mb=0.0)

    assert result["vacuumed"] is True
    assert result["size_before"] > result["size_after"], result
    assert result["reclaimed_bytes"] > 0, result


def test_the_vacuum_runs_outside_any_transaction(file_db):
    """``VACUUM`` cannot run inside a transaction — so the connection must be in
    AUTOCOMMIT when it goes out.

    This is asserted rather than left to luck because it currently WORKS without
    AUTOCOMMIT: pysqlite's legacy implicit-BEGIN only fires for INSERT/UPDATE/DELETE, so
    a bare ``VACUUM`` slips through today. That is a property of one driver version, not
    of the contract — one DML statement added to this connection, or Python's newer
    ``autocommit`` handling, and the vacuum starts raising "cannot VACUUM from within a
    transaction" on a boot nobody is watching.
    """
    from sqlalchemy import event

    from ba2_common.core.db_maintenance import vacuum_if_needed

    _make_freelist(file_db)
    levels = []

    def _record(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("VACUUM"):
            # The DRIVER's own flag, not SQLAlchemy's view of it: ``isolation_level is
            # None`` is precisely "pysqlite will not open an implicit transaction".
            # (``Connection.get_isolation_level()`` is no use here — the SQLite dialect
            # answers it from ``PRAGMA read_uncommitted`` and reports SERIALIZABLE even
            # under AUTOCOMMIT.)
            levels.append(conn.connection.dbapi_connection.isolation_level)

    event.listen(file_db, "before_cursor_execute", _record)
    try:
        assert vacuum_if_needed(min_free_mb=0.0)["vacuumed"] is True
    finally:
        event.remove(file_db, "before_cursor_execute", _record)

    assert levels == [None], f"the VACUUM ran with the driver able to open a transaction: {levels!r}"


def test_an_unmeasurable_database_size_is_unknown_and_not_zero(file_db, monkeypatch):
    """The tri-state, which this codebase has paid for repeatedly.

    A size we could not read must come back as ``None``. Coerced to ``0`` it becomes a
    measurement — "the database is empty" — and every consumer (the banner, the log line)
    then reports a confident, wrong, and specifically SMALLER number.
    """
    from ba2_common.core import db_maintenance

    def _boom(path):
        raise OSError("Input/output error")

    monkeypatch.setattr(db_maintenance.os.path, "getsize", _boom)
    assert db_maintenance.database_file_size_bytes() is None


def test_an_in_memory_database_is_never_vacuumed():
    """A backtest's ``:memory:`` DB has no file to reclaim; rewriting it is pure cost."""
    import ba2_common.core.db as pkg_db
    from ba2_common.core.db_maintenance import vacuum_if_needed

    saved = pkg_db._engine
    pkg_db._engine = pkg_db._build_engine(":memory:")
    try:
        result = vacuum_if_needed(min_free_mb=0.0)
    finally:
        pkg_db._engine.dispose()
        pkg_db._engine = saved
    assert result["vacuumed"] is False


# ---------------------------------------------------------------------------
# FAILURE MUST NEVER BLOCK STARTUP — AND MUST NEVER BE SILENT
# ---------------------------------------------------------------------------

def test_a_failing_purge_does_not_prevent_startup_and_logs_at_error(file_db, monkeypatch):
    from ba2_common.core import db_maintenance

    errors = _capture_errors(monkeypatch, db_maintenance)

    def _explode(days_to_keep):
        raise RuntimeError("disk is full")

    result = db_maintenance.run_startup_maintenance(_explode, min_free_mb=1_000_000)

    assert result["purge"] is None
    assert any("disk is full" in m for m in errors), errors


def test_a_purge_that_reports_an_error_is_logged_at_error(file_db, monkeypatch):
    """``cleanup_activity_logs`` swallows its own exception and returns ``{'error': ...}``.
    A caller that only looks for a raised exception would treat that as success."""
    from ba2_common.core import db_maintenance

    errors = _capture_errors(monkeypatch, db_maintenance)
    result = db_maintenance.run_startup_maintenance(
        lambda days_to_keep: {"deleted_count": 0, "error": "database is locked"},
        min_free_mb=1_000_000,
    )

    assert any("database is locked" in m for m in errors), errors
    assert result["purge"]["error"] == "database is locked"


def test_a_bad_retention_setting_does_not_prevent_startup(file_db, monkeypatch):
    from ba2_common.core import db_maintenance

    monkeypatch.setenv(db_maintenance.ACTIVITY_LOG_RETENTION_DAYS_ENV, "not-a-number")
    errors = _capture_errors(monkeypatch, db_maintenance)
    called = []

    result = db_maintenance.run_startup_maintenance(
        lambda days_to_keep: called.append(days_to_keep), min_free_mb=1_000_000)

    assert called == [], "a refused retention must not be turned into a guess and run"
    assert result["purge"] is None
    assert any("not-a-number" in m or "retention" in m.lower() for m in errors), errors


def test_a_failing_vacuum_does_not_prevent_startup_and_logs_at_error(file_db, monkeypatch):
    from ba2_common.core import db_maintenance

    _make_freelist(file_db)
    errors = _capture_errors(monkeypatch, db_maintenance)
    monkeypatch.setattr(db_maintenance, "_VACUUM_SQL", "VACUUM no_such_schema")

    result = db_maintenance.run_startup_maintenance(_purge, min_free_mb=0.0)

    assert result["vacuum"] is None
    assert any("vacuum" in m.lower() for m in errors), errors


def test_a_failing_vacuum_leaves_no_transaction_open(file_db, monkeypatch):
    """The blast radius this whole placement exists to avoid.

    ``VACUUM`` cannot run inside a transaction and takes an EXCLUSIVE lock. If a failed
    attempt returned its connection to the pool with a write transaction still open, the
    next writer would block on a lock nobody is going to release — the same family of
    self-deadlock that cost two live trades this month, with a bigger radius.

    ``BEGIN IMMEDIATE`` from an INDEPENDENT connection with a 0.5s timeout is the direct
    question: can anyone else take the write lock right now?
    """
    from ba2_common.core import db_maintenance

    _make_freelist(file_db)
    monkeypatch.setattr(db_maintenance, "_VACUUM_SQL", "VACUUM no_such_schema")
    db_maintenance.run_startup_maintenance(_purge, min_free_mb=0.0)

    probe = sqlite3.connect(_db_path(file_db), timeout=0.5)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
    finally:
        probe.close()

    # The other half of the same failure: a connection that is never given back.
    # ``BEGIN IMMEDIATE`` above cannot see that (a checked-out connection holding no
    # lock blocks nobody until the pool runs dry, twenty boots later), so ask the pool.
    assert file_db.pool.checkedout() == 0, \
        "the failed vacuum kept its connection checked out of the pool"

    # ...and the ORM path still writes.
    _add_logs(file_db, [1])
    assert _count(file_db, ActivityLog) >= 1


def test_a_failing_purge_does_not_stop_the_vacuum(file_db, monkeypatch):
    """Two independent jobs. Reclaimable space is still reclaimable when the purge broke."""
    from ba2_common.core import db_maintenance

    _make_freelist(file_db)
    _capture_errors(monkeypatch, db_maintenance)

    result = db_maintenance.run_startup_maintenance(
        lambda days_to_keep: (_ for _ in ()).throw(RuntimeError("boom")), min_free_mb=0.0)

    assert result["purge"] is None
    assert result["vacuum"]["vacuumed"] is True


# ---------------------------------------------------------------------------
# THE WIRING: main.py
# ---------------------------------------------------------------------------

def test_the_startup_hook_purges_through_the_settings_cleanup_function(file_db, monkeypatch):
    """``main.startup_db_maintenance()`` end to end, on a throwaway file DB.

    It must go through ``core.cleanup.cleanup_activity_logs`` — the SAME function the
    Settings → Cleanup tab calls. Two implementations of "delete activity logs older than
    N days" is exactly the drift this codebase keeps paying for.

    THE ONE TEST HERE THAT USES THE REAL CLOCK, because the production hook takes no
    ``now`` and inventing a parameter only tests could pass would be testing the wrong
    thing. It is still not measuring the window: the two rows are 400 days and 1 day old,
    399 days apart, so the assertion holds for ANY retention between 2 and 399 days and
    cannot pass or fail by coincidence of today's date. The window itself is pinned to
    the microsecond by the frozen tests above.
    """
    import main
    from ba2_common.core import db_maintenance

    monkeypatch.delenv(db_maintenance.ACTIVITY_LOG_RETENTION_DAYS_ENV, raising=False)
    real_now = datetime.now(timezone.utc)
    with Session(file_db) as session:
        for age in (400, 1):
            session.add(ActivityLog(
                created_at=real_now - timedelta(days=age),
                severity=ActivityLogSeverity.INFO,
                type=ActivityLogType.APPLICATION_STATUS_CHANGE,
                description=f"age={age}", data={}))
        session.commit()

    result = main.startup_db_maintenance()

    assert _log_descriptions(file_db) == ["age=1"]
    assert result["purge"]["deleted_count"] == 1


def test_the_startup_hook_never_raises(file_db, monkeypatch):
    import main
    from ba2_trade_platform.core import cleanup

    monkeypatch.setattr(cleanup, "cleanup_activity_logs",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    from ba2_common.core import db_maintenance
    _capture_errors(monkeypatch, db_maintenance)

    main.startup_db_maintenance()  # must not raise


# ---------------------------------------------------------------------------
# PLACEMENT: before anything that schedules work
# ---------------------------------------------------------------------------

#: Every call in ``initialize_system()`` that starts a thread, a scheduler or a queue.
#: ``VACUUM`` holds an EXCLUSIVE lock on the whole database, so all of these must
#: already be quiet when it runs.
SCHEDULING_CALLS = (
    "force_sync_all_transactions",       # first real DB traffic
    "get_job_manager",
    "clear_running_analysis_on_startup",
    "execute_account_refresh_immediately",
    "start",                             # job_manager.start()
    "initialize_worker_queue",
    "initialize_smart_risk_manager_queue",
    "get_instrument_auto_adder",
)


def _initialize_system_call_order():
    """``[(lineno, call name), ...]`` for every call in ``main.initialize_system``."""
    tree = ast.parse((REPO_ROOT / "main.py").read_text())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "initialize_system")
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name:
                out.append((node.lineno, name))
    return sorted(out)


def test_the_purge_runs_before_anything_that_schedules_work():
    order = _initialize_system_call_order()
    names = [name for _, name in order]

    assert "startup_db_maintenance" in names, \
        "initialize_system() must run the startup maintenance pass"

    maintenance_line = min(ln for ln, name in order if name == "startup_db_maintenance")

    for scheduler in SCHEDULING_CALLS:
        lines = [ln for ln, name in order if name == scheduler]
        assert lines, f"{scheduler}() vanished from initialize_system(); update this test"
        assert maintenance_line < min(lines), (
            f"the startup VACUUM takes an EXCLUSIVE lock on the whole database and must "
            f"run before {scheduler}() (line {min(lines)}), not after (line {maintenance_line})"
        )


def test_the_purge_runs_after_the_db_seam_is_wired_and_the_schema_exists():
    """``wire_all_seams()`` configures the engine and MUST precede any DB touch;
    ``init_db()`` is what guarantees ``activitylog`` exists at all."""
    order = _initialize_system_call_order()
    lines = [ln for ln, name in order if name == "startup_db_maintenance"]
    assert lines, "initialize_system() must run the startup maintenance pass"
    maintenance_line = min(lines)

    for prerequisite in ("wire_all_seams", "init_db"):
        lines = [ln for ln, name in order if name == prerequisite]
        assert lines, f"{prerequisite}() vanished from initialize_system(); update this test"
        assert maintenance_line > max(lines), \
            f"the maintenance pass must run AFTER {prerequisite}()"
