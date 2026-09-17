"""GET /api/dashboard/stats must never read the backtest blobs and must be served by the covering
indexes (db_migrate/031). Background: the ORM `.all()` it used loaded 20 x ~10 MB rows after an
18 s / 2-min scan, on the event loop, freezing every route (2026-09-02 diagnosis).
"""
from __future__ import annotations

import itertools
import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

_BLOB_RE = re.compile(r"\b(trades|equity_curve|drawdown_curve|results|all_results)\b", re.I)

# EXPLICIT, STRICTLY INCREASING created_at for every seeded row. Both models default it to
# ``server_default=func.now()``, and SQLite's CURRENT_TIMESTAMP has ONE-SECOND resolution, so a
# whole seeding run usually lands on a single timestamp -- and the route sorts its activity list
# by that timestamp with a stable sort that keeps backtests (appended first) ahead of the
# optimizations on every tie. The stats test then passed or failed on whether the seeding happened
# to cross a second boundary: adding unrelated test FILES ahead of this one in the session was
# enough to flip it. A far-future base also keeps rows other tests in the session left behind out
# of the newest-20 window.
_SEED_CLOCK = itertools.count()
_SEED_BASE = datetime(2030, 1, 1)


def _stamp():
    return _SEED_BASE + timedelta(seconds=next(_SEED_CLOCK))


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
    # Without this, commit() expires the returned objects and the assertions' own `row.id` reads
    # fire a full-row refresh SELECT (blobs and all) into `captured_sql` — seed noise that has
    # nothing to do with the route under test.
    db.expire_on_commit = False
    rows = []
    for i in range(n):
        bt = Backtest(
            name=f"bt-{i}", engine_type="daily_expert", status="completed", created_at=_stamp(),
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
    db.expire_on_commit = False  # see _seed_backtests: keeps refresh SELECTs out of captured_sql
    s = Strategy(name="dash-opt-strategy", entry_rules=[], exit_rules=[])
    db.add(s)
    db.commit()
    rows = []
    for i in range(n):
        o = StrategyOptimization(strategy_id=s.id, name=f"opt-{i}", status="completed",
                                 fitness_metric="sharpe", optimization_type="genetic",
                                 created_at=_stamp(), all_results=[{"fitness": 1.0}] * 50)
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
