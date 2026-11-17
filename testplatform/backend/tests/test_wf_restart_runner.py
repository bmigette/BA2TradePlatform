"""The real runner's side of walk-forward restarts: ``RealRunner.train_best_fitness`` reads the GA best
fitness of a completed job (how a fold's restarts are ranked) and refuses a missing one. Hermetic: the
stub optimization of test_strategy_optimization_handler on the isolated test DB; nothing is optimized
for real."""
from __future__ import annotations

import pytest

from app.models.database import Base, SessionLocal, engine
from app.models.strategy_optimization import StrategyOptimization
from app.services import strategy_optimization_handler as H
from app.services import walk_forward as WF
from tests import test_strategy_optimization_handler as T
from tests.test_walk_forward import _tool
from tests.test_wf_chain_seed import _rename, _run


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr(H, "_run_trial_backtest", T._deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})


def _runner(monkeypatch):
    rr = _tool().RealRunner(None)
    monkeypatch.setattr(rr, "_tool", lambda: None)
    return rr


def test_best_fitness_is_the_completed_jobs_own(monkeypatch):
    sid = T._seed_strategy()
    opt_id, out = _run(sid, "rst-wf1-r2", populationSize=6, generations=2)
    assert out["status"] == "completed", out
    want = T._load_opt(opt_id).best_fitness
    assert _runner(monkeypatch).train_best_fitness("rst-wf1-r2") == pytest.approx(want)


def test_a_job_that_is_not_completed_or_has_no_fitness_is_refused(monkeypatch):
    sid = T._seed_strategy()
    pending = T._seed_opt(sid)
    _rename(pending, "rst-pending")
    rr = _runner(monkeypatch)
    with pytest.raises(WF.WalkForwardError, match="not completed"):
        rr.train_best_fitness("rst-pending")
    db = SessionLocal()
    try:
        row = db.get(StrategyOptimization, pending)
        row.status, row.best_fitness = "completed", None
        db.commit()
    finally:
        db.close()
    with pytest.raises(WF.MissingMetric, match="best_fitness"):
        rr.train_best_fitness("rst-pending")
    with pytest.raises(WF.WalkForwardError, match="0 optimizations"):
        rr.train_best_fitness("rst-nothing")
