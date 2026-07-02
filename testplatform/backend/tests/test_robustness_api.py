"""Task 4: robustness REST endpoints (launch + poll) for MC and schedule variants.

This test uses the REAL module-level ``engine``/``SessionLocal`` (bound by the package conftest to a
throwaway isolated sqlite via DATABASE_URL) — NOT the shared gate-engine ``client``/``db`` fixtures —
because the robustness_handler opens its OWN ``SessionLocal()`` session (it may run inline in the
request OR on a worker). Both the route's ``get_db`` and the handler therefore share the same engine,
mirroring production. This mirrors ``test_optimize_route.py``.

MC runs INLINE in the request (sub-second, over the parent's persisted trades) so a client that POSTs
then GETs immediately sees ``completed`` + percentile bands. Schedule variants are queued on the
dedicated re-run pool (``get_rerun_task_queue``), stubbed here.
"""
from __future__ import annotations

from datetime import datetime

import pytest


@pytest.fixture(scope="module")
def _tables():
    from app.models.database import engine, Base
    import app.models  # noqa: F401 — registers all models on Base.metadata

    Base.metadata.create_all(bind=engine)
    yield engine


@pytest.fixture
def db(_tables):
    from app.models.database import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(_tables):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


def _seed_backtest(
    db,
    *,
    name="rbst-parent",
    engine_type="daily_expert",
    with_trades=True,
    expert_name="FMPRating",
):
    from app.models.backtest import Backtest

    trades = None
    if with_trades:
        trades = [
            {"symbol": "AAPL", "pnl_pct": 3.5, "entry_time": "2020-01-05", "exit_time": "2020-01-20"},
            {"symbol": "MSFT", "pnl_pct": -1.2, "entry_time": "2020-02-01", "exit_time": "2020-02-15"},
            {"symbol": "GOOG", "pnl_pct": 5.0, "entry_time": "2020-03-01", "exit_time": "2020-03-20"},
            {"symbol": "AAPL", "pnl_pct": 2.1, "entry_time": "2020-04-01", "exit_time": "2020-04-15"},
            {"symbol": "TSLA", "pnl_pct": -0.8, "entry_time": "2020-05-01", "exit_time": "2020-05-15"},
        ]
    bt = Backtest(
        name=name,
        expert_name=expert_name,
        engine_type=engine_type,
        strategy_params={"foo": "bar"},
        start_date=datetime(2020, 1, 1),
        end_date=datetime(2020, 12, 31),
        initial_capital=10000.0,
        status="completed",
        trades=trades,
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)
    return bt


# --- POST /robustness (MC inline) -------------------------------------------
def test_post_monte_carlo_runs_inline(client, db):
    bt = _seed_backtest(db)
    payload = {
        "backtest_ids": [bt.id],
        "monte_carlo": {
            "enabled": True,
            "n_paths": 200,
            "seed": 42,
            "methods": ["bootstrap", "shuffle"],
            "drop_k": [1, 2],
            "jitter_bp": 0,
        },
        "schedule": {"enabled": False},
    }
    resp = client.post("/api/backtests/robustness", json=payload)
    assert resp.status_code == 200, resp.text
    runs = resp.json()["runs"]
    assert len(runs) == 1
    run = runs[0]
    assert run["backtest_id"] == bt.id
    assert run["kind"] == "monte_carlo"
    assert run["status"] == "completed"
    run_id = run["robustness_run_id"]
    assert isinstance(run_id, int)

    # GET single run shows completed + percentile bands
    resp2 = client.get(f"/api/backtests/robustness/{run_id}")
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    assert body["status"] == "completed"
    results = body["results"]
    assert "methods" in results
    assert "bootstrap" in results["methods"]
    band = results["methods"]["bootstrap"]["annualized_return"]
    for pk in ("p5", "p50", "p95"):
        assert pk in band
    assert len(results["drop_k"]) == 2


def test_get_list_for_backtest(client, db):
    bt = _seed_backtest(db)
    payload = {
        "backtest_ids": [bt.id],
        "monte_carlo": {
            "enabled": True,
            "n_paths": 100,
            "seed": 7,
            "methods": ["bootstrap"],
            "drop_k": [1],
            "jitter_bp": 0,
        },
        "schedule": {"enabled": False},
    }
    r = client.post("/api/backtests/robustness", json=payload)
    assert r.status_code == 200, r.text

    resp = client.get(f"/api/backtests/robustness?backtest_id={bt.id}")
    assert resp.status_code == 200, resp.text
    runs = resp.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["kind"] == "monte_carlo"
    assert runs[0]["status"] == "completed"
    assert runs[0]["results"]["n_trades"] == 5


# --- POST /robustness (schedule queued) -------------------------------------
def test_post_schedule_creates_variants(client, db, monkeypatch):
    bt = _seed_backtest(db)

    queued: list = []

    class _StubQueue:
        def queue_task(self, *a, **kw):
            queued.append(kw.get("payload"))
            return "stub-rerun-task"

    import app.services.robustness_handler as rh

    # rebuild_config_for_backtest validates reconstructibility; stub it so we don't need a real
    # expert/universe round-trip in the unit test.
    monkeypatch.setattr(rh, "rebuild_config_for_backtest", lambda bt, db: {"ok": True})
    monkeypatch.setattr(rh, "get_rerun_task_queue", lambda: _StubQueue())

    payload = {
        "backtest_ids": [bt.id],
        "monte_carlo": {"enabled": False},
        "schedule": {
            "enabled": True,
            "day_variants": True,
            "time_variants": ["10:30", "12:30"],
        },
    }
    resp = client.post("/api/backtests/robustness", json=payload)
    assert resp.status_code == 200, resp.text
    runs = resp.json()["runs"]
    assert len(runs) == 1
    run = runs[0]
    assert run["kind"] == "schedule"
    run_id = run["robustness_run_id"]

    # 5 day variants + 2 time variants = 7 queued + 7 variant rows
    assert len(queued) == 7

    from app.models.backtest import RobustnessRun, Backtest

    rr = db.query(RobustnessRun).filter(RobustnessRun.id == run_id).first()
    assert rr is not None
    assert len(rr.variant_backtest_ids) == 7
    variants = db.query(Backtest).filter(Backtest.id.in_(rr.variant_backtest_ids)).all()
    assert len(variants) == 7
    for v in variants:
        assert v.name.startswith("RBST-")
        assert v.is_saved is False


# --- Guards -----------------------------------------------------------------
def test_unknown_backtest_404(client, db):
    payload = {
        "backtest_ids": [999999],
        "monte_carlo": {"enabled": True, "n_paths": 10, "seed": 1, "methods": ["bootstrap"],
                        "drop_k": [1], "jitter_bp": 0},
        "schedule": {"enabled": False},
    }
    resp = client.post("/api/backtests/robustness", json=payload)
    assert resp.status_code == 404, resp.text


def test_schedule_on_non_daily_expert_400(client, db):
    bt = _seed_backtest(db, engine_type="ml")
    payload = {
        "backtest_ids": [bt.id],
        "monte_carlo": {"enabled": False},
        "schedule": {"enabled": True, "day_variants": True, "time_variants": []},
    }
    resp = client.post("/api/backtests/robustness", json=payload)
    assert resp.status_code == 400, resp.text


def test_mc_on_empty_trades_400(client, db):
    bt = _seed_backtest(db, with_trades=False)
    payload = {
        "backtest_ids": [bt.id],
        "monte_carlo": {"enabled": True, "n_paths": 10, "seed": 1, "methods": ["bootstrap"],
                        "drop_k": [1], "jitter_bp": 0},
        "schedule": {"enabled": False},
    }
    resp = client.post("/api/backtests/robustness", json=payload)
    assert resp.status_code == 400, resp.text


def test_get_unknown_run_404(client, db):
    resp = client.get("/api/backtests/robustness/424242")
    assert resp.status_code == 404, resp.text
