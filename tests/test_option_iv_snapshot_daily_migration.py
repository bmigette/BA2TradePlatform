"""The one-sample-per-day unique index on option_iv_snapshot, run for real.

``record_atm_iv`` enforces the invariant in Python. This index is the second lock:
``get_iv_rank`` averages an UNWEIGHTED window, so any writer that slips past the
Python guard (a manual backfill script, a second process racing the daily job)
silently reweights a 252-day percentile toward the last few days -- underneath nine
live option rules. A guard with no DB backing is a convention, not an invariant.

Same importlib approach as tests/test_instrument_unique_migration.py: the base schema
comes from SQLModel.metadata.create_all, not from an initial migration, so
`alembic upgrade` from an empty file cannot reach this revision. Every test uses its
own tmp_path database; the production DB is never opened.
"""
import importlib.util
import os

import pytest
import sqlalchemy
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_PATH = os.path.join(
    REPO, "alembic", "versions", "a3f1c07d9e21_option_iv_snapshot_one_per_day.py")

_CREATE = (
    "CREATE TABLE option_iv_snapshot ("
    " id INTEGER NOT NULL,"
    " account_id INTEGER NOT NULL,"
    " underlying VARCHAR NOT NULL,"
    " atm_iv FLOAT NOT NULL,"
    " recorded_at DATETIME NOT NULL,"
    " PRIMARY KEY (id))"
)
_INSERT = text("INSERT INTO option_iv_snapshot (id, account_id, underlying, atm_iv, recorded_at)"
               " VALUES (:id, :a, :u, :iv, :t)")


def _load():
    spec = importlib.util.spec_from_file_location("iv_snapshot_daily_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db(tmp_path, rows):
    engine = create_engine(f"sqlite:///{tmp_path / 'iv.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text(_CREATE))
        for r in rows:
            conn.execute(_INSERT, {"id": r[0], "a": r[1], "u": r[2], "iv": r[3], "t": r[4]})
    return engine


def _upgrade(engine):
    module = _load()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn, opts={"as_batch": True})
        module.op = Operations(ctx)
        module.sa = sqlalchemy
        module.upgrade()
    return module


def _rows(engine):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT id, account_id, underlying, atm_iv FROM option_iv_snapshot ORDER BY id"))]


def test_index_rejects_a_second_sample_on_the_same_day(tmp_path):
    engine = _db(tmp_path, [(1, 5, "AAPL", 0.25, "2026-08-20 20:30:00.000000")])
    _upgrade(engine)

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(_INSERT, {"id": 2, "a": 5, "u": "AAPL", "iv": 0.90,
                                   "t": "2026-08-20 20:35:00.000000"})


@pytest.mark.parametrize("row,why", [
    ((2, 5, "AAPL", 0.30, "2026-08-21 20:30:00.000000"), "a NEW DAY must still be recordable"),
    ((2, 5, "MSFT", 0.30, "2026-08-20 20:30:00.000000"), "a different UNDERLYING is its own series"),
    ((2, 9, "AAPL", 0.30, "2026-08-20 20:30:00.000000"), "a different ACCOUNT is its own series"),
])
def test_index_permits_legitimate_rows(tmp_path, row, why):
    engine = _db(tmp_path, [(1, 5, "AAPL", 0.25, "2026-08-20 20:30:00.000000")])
    _upgrade(engine)
    with engine.begin() as conn:
        conn.execute(_INSERT, {"id": row[0], "a": row[1], "u": row[2], "iv": row[3], "t": row[4]})
    assert len(_rows(engine)) == 2, why


def test_upgrade_collapses_pre_existing_duplicates_keeping_the_first(tmp_path):
    """A 5-minute-cadence writer would already have left ~288 rows/day behind. The
    migration must be able to run on that database, and the row it keeps must be the
    one record_atm_iv would have returned (the first of the day)."""
    engine = _db(tmp_path, [
        (1, 5, "AAPL", 0.25, "2026-08-20 13:35:00.000000"),   # first of the day -> kept
        (2, 5, "AAPL", 0.31, "2026-08-20 13:40:00.000000"),
        (3, 5, "AAPL", 0.44, "2026-08-20 20:55:00.000000"),
        (4, 5, "AAPL", 0.28, "2026-08-21 13:35:00.000000"),   # next day -> kept
        (5, 5, "MSFT", 0.60, "2026-08-20 13:35:00.000000"),   # other symbol -> kept
    ])
    _upgrade(engine)
    assert _rows(engine) == [(1, 5, "AAPL", 0.25), (4, 5, "AAPL", 0.28), (5, 5, "MSFT", 0.60)]


def test_downgrade_drops_the_index_and_reallows_duplicates(tmp_path):
    engine = _db(tmp_path, [(1, 5, "AAPL", 0.25, "2026-08-20 20:30:00.000000")])
    module = _upgrade(engine)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn, opts={"as_batch": True})
        module.op = Operations(ctx)
        module.downgrade()
    with engine.begin() as conn:
        conn.execute(_INSERT, {"id": 2, "a": 5, "u": "AAPL", "iv": 0.90,
                               "t": "2026-08-20 20:35:00.000000"})
    assert len(_rows(engine)) == 2
