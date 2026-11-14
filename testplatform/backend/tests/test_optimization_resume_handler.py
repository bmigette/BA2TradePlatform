"""End to end through ``handle_strategy_optimization``: a job killed mid-generation and resumed
the way ``optimize --resume-interrupted`` does it (retire the dead row, new row of the same name,
``resume_of``) finishes with the SAME result as the uninterrupted job: best genome and fitness,
final population (the chain seed of the next fold), and the same set of evaluated trials in
``all_results`` (the walk-forward top-N is selected from it), without re-running a trial that
had finished before the kill.

The trial is the deterministic stub of test_strategy_optimization_handler; the kill is a
BaseException raised inside a trial, which the handler does not catch (a SIGKILL leaves the row
``running`` just like this).
"""
from __future__ import annotations

import os

import pytest

from app.models.database import Base, SessionLocal, engine
from app.models.strategy_optimization import (StrategyOptimization, StrategyOptimizationPopulation,
                                              StrategyOptimizationRunner)
from app.services import optimization_resume as R
from app.services import strategy_optimization_handler as H

from .test_strategy_optimization_handler import _deterministic_stub, _ga_config

pytest.importorskip("deap")


@pytest.fixture(scope="module", autouse=True)
def _db():
    Base.metadata.create_all(bind=engine)


class _Killed(BaseException):
    pass


def _counting_stub(calls, kill_at=None):
    def stub(cfg, hoisted, decoded):
        calls.append(1)
        if kill_at is not None and len(calls) == kill_at:
            raise _Killed()
        return _deterministic_stub(cfg, hoisted, decoded)
    return stub


def _seed_wide_strategy() -> int:
    """The stub strategy of test_strategy_optimization_handler with WIDE ranges (100 x 50
    points): the 66-point original converges in a dozen trials, too few to kill a run mid-way."""
    from app.models.strategy import Strategy
    db = SessionLocal()
    try:
        s = Strategy(name="opt-resume-test", entry_rules=[{
            "id": "bracket", "conditions": None, "continue_processing": False,
            "actions": [
                {"action_type": "buy"},
                {"id": "e_tp", "action_type": "adjust_take_profit",
                 "reference_value": "order_open_price", "action_value": 5.0,
                 "action_value_optimize": True, "action_value_min": 1.0,
                 "action_value_max": 100.0, "action_value_step": 1.0},
                {"id": "e_sl", "action_type": "adjust_stop_loss",
                 "reference_value": "order_open_price", "action_value": -2.0,
                 "action_value_optimize": True, "action_value_min": -50.0,
                 "action_value_max": -1.0, "action_value_step": 1.0},
            ]}])
        db.add(s); db.commit(); db.refresh(s)
        return s.id
    finally:
        db.close()


def _new_row(strategy_id, name, config):
    db = SessionLocal()
    try:
        row = StrategyOptimization(strategy_id=strategy_id, name=name, fitness_metric="sharpe",
                                   optimization_type="genetic", optimization_config=config,
                                   status="pending")
        db.add(row); db.commit(); db.refresh(row)
        return row.id
    finally:
        db.close()


def _result(opt_id):
    db = SessionLocal()
    try:
        row = db.get(StrategyOptimization, opt_id)
        pop = db.get(StrategyOptimizationPopulation, opt_id)
        keys = sorted(r["key"] for r in row.all_results)
        return {"status": row.status, "best_params": row.best_params,
                "best_fitness": row.best_fitness, "keys": keys,
                "population": pop.population if pop else None}
    finally:
        db.close()


def _make_dead(opt_id):
    """What a SIGKILL leaves: the recorded runner process no longer exists."""
    import subprocess
    import sys
    import psutil
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    started = psutil.Process(p.pid).create_time()
    p.wait()
    db = SessionLocal()
    try:
        run = db.get(StrategyOptimizationRunner, opt_id)
        run.pid, run.pid_started = p.pid, started
        db.commit()
    finally:
        db.close()


def _config():
    return _ga_config(populationSize=12, generations=8, earlyStoppingGenerations=99,
                      mutationProb=0.6, crossoverProb=0.9)        # 30 trials, 12 in generation 0


def _interrupt_and_resume(monkeypatch, name, kill_at):
    """Run job ``name`` until trial ``kill_at`` kills it, then resume it as the launcher does.
    Returns (killed row id, resumed row id, trials run by the resumed process)."""
    sid = _seed_wide_strategy()
    k_calls = []
    monkeypatch.setattr(H, "_run_trial_backtest", _counting_stub(k_calls, kill_at))
    killed = _new_row(sid, name, _config())
    with pytest.raises(_Killed):
        H.handle_strategy_optimization("t-kill", {"optimization_id": killed})
    db = SessionLocal()
    try:
        assert db.get(StrategyOptimization, killed).status == "running"
        assert db.get(StrategyOptimizationRunner, killed).pid == os.getpid()
    finally:
        db.close()
    _make_dead(killed)

    db = SessionLocal()
    try:
        a = R.assess(db, name)
        assert (a.kind, a.opt_id) == ("resume", killed)
        R.retire_optimization(db, killed, "resumed by the next row of this name")
    finally:
        db.close()
    resumed = _new_row(sid, name, _config())
    r_calls = []
    monkeypatch.setattr(H, "_run_trial_backtest", _counting_stub(r_calls))
    out = H.handle_strategy_optimization("t-resume", {"optimization_id": resumed,
                                                      "resume_of": killed})
    assert out["status"] == "completed", out
    return killed, resumed, len(k_calls) - 1, len(r_calls)


@pytest.mark.parametrize("kill_at", [3, 18, 27])      # generation 0, mid-run, late
def test_resumed_job_equals_the_uninterrupted_job(monkeypatch, kill_at):
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    tag = f"{os.getpid()}-{kill_at}"
    u_calls = []
    monkeypatch.setattr(H, "_run_trial_backtest", _counting_stub(u_calls))
    whole = _new_row(_seed_wide_strategy(), f"res-whole-{tag}", _config())
    assert H.handle_strategy_optimization("t-whole", {"optimization_id": whole})["status"] == \
        "completed"
    want = _result(whole)

    killed, resumed, done_before_kill, run_after = _interrupt_and_resume(
        monkeypatch, f"res-cut-{tag}", kill_at)
    got = _result(resumed)
    assert got == {**want, "status": "completed"}
    # nothing that finished before the kill ran again
    assert done_before_kill + run_after == len(u_calls)

    db = SessionLocal()
    try:
        old = db.get(StrategyOptimization, killed)
        assert old.name == f"res-cut-{tag}~interrupted-{killed}" and old.status == "failed"
        assert db.get(StrategyOptimizationRunner, resumed).resumed_from_id == killed
        assert R.assess(db, f"res-cut-{tag}").kind == "done" or \
            R.assess(db, f"res-cut-{tag}").kind == "topn"
    finally:
        db.close()


def test_a_linked_resume_refuses_a_checkpoint_of_another_gene_space(monkeypatch):
    """The plain path DISCARDS such a checkpoint and starts from generation 0; a resume that was
    asked for must fail loudly instead."""
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    name = f"res-fp-{os.getpid()}"
    sid = _seed_wide_strategy()
    calls = []
    monkeypatch.setattr(H, "_run_trial_backtest", _counting_stub(calls, 15))
    killed = _new_row(sid, name, _config())
    with pytest.raises(_Killed):
        H.handle_strategy_optimization("t-kill", {"optimization_id": killed})
    _make_dead(killed)
    tid = H.checkpoint_task_id(name, killed)
    ckpt = H._load_checkpoint(tid)
    assert ckpt is not None
    H._save_checkpoint(tid, {**ckpt, "fingerprint": "another-space"})
    db = SessionLocal()
    try:
        R.retire_optimization(db, killed, "test")
    finally:
        db.close()
    resumed = _new_row(sid, name, _config())
    monkeypatch.setattr(H, "_run_trial_backtest", _counting_stub([]))
    out = H.handle_strategy_optimization("t-resume", {"optimization_id": resumed,
                                                      "resume_of": killed})
    assert out["status"] == "failed"
    assert "fingerprint" in out["error"] and "another-space" in out["error"]
