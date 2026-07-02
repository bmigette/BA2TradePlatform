"""Task 3: robustness handler service.

Covers:
  * ``run_monte_carlo_for_backtest`` — runs the pure-function MC over a parent's persisted
    ``trades`` and persists percentile bands + drop-K on the RobustnessRun (status completed).
    Fail-soft: a broken parent row sets status=failed + error_message and does NOT raise.
  * ``launch_schedule_variants`` — clones the parent's ORIGINAL config (via the shared
    ``rebuild_config_for_backtest`` helper extracted from the rerun handler), creates one NEW
    pending Backtest row per schedule variant with the correct ``run_schedule_override`` override
    + ``RBST-<variant>-<name>`` name, records the ids, enqueues one rerun task per variant, and
    NEVER mutates the parent row.
  * ``collect_schedule_results`` — snapshots variant headline metrics once all variants are
    terminal and marks the run completed.
  * ``rebuild_config_for_backtest`` — returns a config equivalent to the existing rerun path.

Uses a throwaway sqlite engine (module-scoped) and monkeypatches the module-level SessionLocal so
the handler's own ``SessionLocal()`` calls hit the test DB, plus a STUB rerun queue that captures
enqueued task payloads.

Run from the backend dir:
    ~/ba2-venvs/test/bin/python -m pytest tests/test_robustness_handler.py -v
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 — register all model classes on Base.metadata
from app.models.database import Base
from app.models.backtest import Backtest, RobustnessRun


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("robustdb") / "robust.sqlite"
    eng = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def Session(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def patch_session(monkeypatch, Session):
    """Point the handler module's SessionLocal at the test engine."""
    import app.services.robustness_handler as H
    monkeypatch.setattr(H, "SessionLocal", Session, raising=True)
    return H


class _StubQueue:
    """Captures ``queue_task`` calls instead of enqueuing on a real worker pool."""

    def __init__(self):
        self.calls = []

    def queue_task(self, task_type, name, payload=None, **kw):
        self.calls.append({"task_type": task_type, "name": name, "payload": payload})
        return f"stub-task-{len(self.calls)}"


@pytest.fixture
def stub_queue(monkeypatch):
    q = _StubQueue()
    import app.services.robustness_handler as H
    monkeypatch.setattr(H, "get_rerun_task_queue", lambda: q, raising=True)
    return q


def _make_parent(session, *, trades, sp=None, name="TEST-parent"):
    bt = Backtest(
        name=name,
        engine_type="daily_expert",
        expert_name="FMPRating",
        optimization_id=None,
        start_date=datetime(2021, 1, 1),
        end_date=datetime(2024, 1, 1),  # 3 years
        initial_capital=10_000.0,
        commission=0.1,
        slippage=0.05,
        status="completed",
        is_saved=True,
        trades=trades,
        strategy_params=sp or {
            "universe": {"mode": "static", "symbols": ["AAPL", "MSFT"]},
            "expertSettings": {},
        },
    )
    session.add(bt)
    session.commit()
    session.refresh(bt)
    return bt


def _make_run(session, backtest_id, kind, params):
    r = RobustnessRun(backtest_id=backtest_id, kind=kind, params=params, status="pending")
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------
def test_run_monte_carlo_persists_results_and_completes(patch_session, Session):
    from app.services import robustness_handler as H

    s = Session()
    trades = [{"pnl_pct": p, "exit_time": f"2022-0{1 + i % 9}-15T00:00:00"}
              for i, p in enumerate([30.0, 2.0, -1.0, 5.0, -3.0, 8.0])]
    bt = _make_parent(s, trades=trades)
    run = _make_run(s, bt.id, "monte_carlo", {
        "methods": ["bootstrap", "shuffle"],
        "n_paths": 50,
        "seed": 42,
        "drop_k": [1, 2],
        "jitter_bp": 0,
    })
    run_id = run.id
    s.close()

    H.run_monte_carlo_for_backtest(run_id)

    s = Session()
    run = s.query(RobustnessRun).get(run_id)
    assert run.status == "completed"
    assert run.completed_at is not None
    res = run.results
    assert res is not None
    # Percentile bands present for each method.
    assert "methods" in res and "bootstrap" in res["methods"]
    band = res["methods"]["bootstrap"]["annualized_return"]
    for key in ("p5", "p25", "p50", "p75", "p95"):
        assert key in band
    # drop-K table present with the requested K values.
    ks = {row["k"] for row in res["drop_k"]}
    assert ks == {1, 2}
    assert res["n_trades"] == 6
    s.close()


def test_run_monte_carlo_fail_soft_sets_failed(patch_session, Session):
    from app.services import robustness_handler as H

    s = Session()
    # No trades on the parent -> MC over an empty trade list should fail-soft (config demands
    # trades) OR at minimum a missing run should not raise. Here we point at a run whose params
    # are missing the required 'seed'/'n_paths' keys so run_monte_carlo raises KeyError.
    bt = _make_parent(s, trades=[{"pnl_pct": 5.0, "exit_time": "2022-01-15T00:00:00"}])
    run = _make_run(s, bt.id, "monte_carlo", {"methods": ["bootstrap"]})  # missing seed/n_paths
    run_id = run.id
    s.close()

    # Must NOT raise.
    H.run_monte_carlo_for_backtest(run_id)

    s = Session()
    run = s.query(RobustnessRun).get(run_id)
    assert run.status == "failed"
    assert run.error_message
    s.close()


def test_run_monte_carlo_unknown_run_is_noop(patch_session):
    from app.services import robustness_handler as H
    # Should not raise on a missing run id.
    H.run_monte_carlo_for_backtest(999_999)


# ---------------------------------------------------------------------------
# Schedule variants
# ---------------------------------------------------------------------------
def test_launch_schedule_variants_creates_rows_and_enqueues(patch_session, Session, stub_queue):
    from app.services import robustness_handler as H

    s = Session()
    bt = _make_parent(s, trades=[{"pnl_pct": 5.0, "exit_time": "2022-01-15T00:00:00"}])
    parent_id = bt.id
    parent_sp_before = dict(bt.strategy_params)
    parent_name = bt.name
    run = _make_run(s, parent_id, "schedule", {
        "day_variants": True,          # Mon..Fri weekly-entry-day
        "time_variants": ["10:30", "12:30", "15:00"],  # entry time shift
    })
    run_id = run.id
    s.close()

    H.launch_schedule_variants(run_id)

    s = Session()
    run = s.query(RobustnessRun).get(run_id)
    # 5 day variants + 3 time variants = 8.
    variant_ids = run.variant_backtest_ids
    assert len(variant_ids) == 8
    assert run.status in ("running", "pending")

    variants = s.query(Backtest).filter(Backtest.id.in_(variant_ids)).all()
    assert len(variants) == 8
    by_name = {v.name: v for v in variants}
    # Day variants: RBST-day-<weekday>-<parent name>.
    for wd in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        name = f"RBST-day-{wd}-{parent_name}"
        assert name in by_name, f"missing {name}"
        ov = by_name[name].strategy_params["runScheduleOverride"]
        assert ov["days"][wd] is True
        assert sum(1 for v in ov["days"].values() if v) == 1  # exactly that day
        assert ov["times"] == ["09:30"]
    # Time variants: RBST-time-<HH:MM>-<parent name>, all days on, single time.
    for t in ("10:30", "12:30", "15:00"):
        name = f"RBST-time-{t}-{parent_name}"
        assert name in by_name, f"missing {name}"
        ov = by_name[name].strategy_params["runScheduleOverride"]
        assert ov["times"] == [t]

    # Every variant row: standalone, unsaved, pending, daily_expert.
    for v in variants:
        assert v.engine_type == "daily_expert"
        assert v.optimization_id is None
        assert v.is_saved is False
        assert v.status == "pending"

    # Parent row is UNCHANGED.
    parent = s.query(Backtest).get(parent_id)
    assert parent.name == parent_name
    assert parent.strategy_params == parent_sp_before
    assert parent.is_saved is True
    assert parent.status == "completed"

    # One enqueue per variant, all rerun_backtest tasks pointing at the variant ids.
    assert len(stub_queue.calls) == 8
    enqueued_ids = {c["payload"]["backtest_id"] for c in stub_queue.calls}
    assert enqueued_ids == set(variant_ids)
    assert all(c["task_type"] == "rerun_backtest" for c in stub_queue.calls)
    s.close()


def test_collect_schedule_results_snapshots_when_terminal(patch_session, Session, stub_queue):
    from app.services import robustness_handler as H

    s = Session()
    bt = _make_parent(s, trades=[{"pnl_pct": 5.0, "exit_time": "2022-01-15T00:00:00"}])
    run = _make_run(s, bt.id, "schedule", {"day_variants": True, "time_variants": []})
    run_id = run.id
    s.close()

    H.launch_schedule_variants(run_id)

    s = Session()
    run = s.query(RobustnessRun).get(run_id)
    variant_ids = list(run.variant_backtest_ids)
    s.close()

    # Not all terminal yet -> collector leaves it non-completed.
    H.collect_schedule_results(run_id)
    s = Session()
    assert s.query(RobustnessRun).get(run_id).status != "completed"
    s.close()

    # Mark all variants completed with headline metrics.
    s = Session()
    for i, vid in enumerate(variant_ids):
        v = s.query(Backtest).get(vid)
        v.status = "completed"
        v.annualized_return = 10.0 + i
        v.max_drawdown = -5.0 - i
        v.calmar_ratio = 2.0
    s.commit()
    s.close()

    H.collect_schedule_results(run_id)

    s = Session()
    run = s.query(RobustnessRun).get(run_id)
    assert run.status == "completed"
    assert run.completed_at is not None
    summary = run.results["schedule_summary"]
    assert len(summary) == len(variant_ids)
    row = summary[0]
    assert "annualized_return" in row and "max_drawdown" in row and "name" in row
    s.close()


# ---------------------------------------------------------------------------
# Shared reconstruction helper
# ---------------------------------------------------------------------------
def test_rebuild_config_matches_rerun_path(patch_session, Session):
    from app.services.backtest import rerun_handler as RH

    s = Session()
    sp = {
        "universe": {"mode": "static", "symbols": ["AAPL", "MSFT"]},
        "expertSettings": {"foo": "bar"},
        "seed": 7,
        "fillModel": "next_bar_open",
    }
    bt = _make_parent(s, trades=[{"pnl_pct": 5.0, "exit_time": "2022-01-15T00:00:00"}], sp=sp)

    via_shared = RH.rebuild_config_for_backtest(bt, s)
    via_rerun = RH.build_rerun_config(s, bt)

    # Same config for a standalone row (both go through _build_standalone_rerun_config).
    assert via_shared == via_rerun
    # Sanity-check load-bearing fields.
    assert via_shared["backtest_id"] == bt.id
    assert via_shared["name"] == bt.name
    s.close()
