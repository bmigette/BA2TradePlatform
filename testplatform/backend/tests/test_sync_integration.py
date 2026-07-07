"""End-to-end: master pushes a Strategy -> StrategyOptimization -> Backtest chain to a real (in
process) worker_server.py instance, then a SEPARATE session against that same DB file confirms
the rows are queryable with correctly remapped local ids — proving the cross-reference resolution
in docs/plans/2026-07-07-remote-result-sync-design.md end to end."""
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

    # A throwaway row consumes id=1 on the MASTER before the real one is created, so the
    # master's strat.id is guaranteed != 1. Without this, both DBs are fresh/empty and the
    # first row in each independently gets id=1 — making `w_strat.id != strat.id` below pass
    # by coincidence rather than actually proving the worker assigned its OWN local id.
    master_db.add(Strategy(name="filler-to-shift-ids", created_at=datetime.now(timezone.utc)))
    master_db.commit()

    strat = Strategy(name="e2e-strat", created_at=datetime.now(timezone.utc))
    master_db.add(strat)
    master_db.commit()
    assert strat.id != 1  # sanity: the filler row above actually shifted the id
    opt = StrategyOptimization(
        name="e2e-opt", strategy_id=strat.id, status="completed",
        fitness_metric="sharpe", optimization_type="genetic",
        created_at=datetime.now(timezone.utc),
    )
    master_db.add(opt)
    master_db.commit()
    bt = Backtest(
        name="TOP1-e2e-opt", engine_type="daily_expert", strategy_id=strat.id,
        optimization_id=opt.id, is_saved=True,
        start_date=datetime(2024, 1, 1), end_date=datetime(2024, 6, 1),
        created_at=datetime.now(timezone.utc),
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
