"""Third review of the resume of interrupted jobs: id reuse, pause vs claim, the plain launch,
abandoning a job, the --submit TOP-N marker, and a kill before the first trial of a remote-only job.
Real handler, launcher functions and resume service; only the backtest (and the remote worker) is a
deterministic stub."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime

import pytest

import app.services.distributed_eval as de
from app.models import Worker
from app.models.backtest import Backtest
from app.models.database import SessionLocal, engine, reconcile_resume_tables
from app.models.strategy_optimization import StrategyOptimization as SO
from app.models.strategy_optimization import StrategyOptimizationRunner as Run
from app.services import optimization_resume as R
from app.services import strategy_optimization_handler as H

from .test_optimization_resume_handler import (  # noqa: F401  (fixtures)
    _Killed, _config, _kill, _launch_resume, _launcher, _make_dead, _new_row, _result, _run,
    _seed_wide_strategy, _uniq, stubbed, whole)

pytest.importorskip("deap")


@pytest.fixture(scope="module", autouse=True)
def _schema():
    from app.models.database import Base
    Base.metadata.create_all(bind=engine)


def _dead_identity():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    ident = R.process_identity(p.pid)
    p.wait()
    return ident


@pytest.fixture(params=["triggers", "no triggers"])
def delete_triggers(request):
    """Migration 033's AFTER DELETE triggers present (init_db path) or absent (a DB created by
    create_all alone): the row binding must hold either way."""
    raw = engine.raw_connection()
    try:
        if request.param == "triggers":
            reconcile_resume_tables(engine)
        else:
            raw.cursor().execute("DROP TRIGGER IF EXISTS trg_runner_follows_optimization")
            raw.cursor().execute("DROP TRIGGER IF EXISTS trg_writer_follows_backtest")
            raw.commit()
        yield request.param
    finally:
        raw.close()
        reconcile_resume_tables(engine)


def _delete_row_only(model, row_id):
    """What an operator does by hand: DELETE the row, nothing else."""
    db = SessionLocal()
    try:
        db.query(model).filter(model.id == row_id).delete()
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------ 1. id reuse


@pytest.mark.parametrize("who", ["same pid", "other pid"])
def test_a_reused_optimization_id_never_inherits_the_deleted_rows_runner(delete_triggers, who):
    name = _uniq("reuse")
    sid = _seed_wide_strategy()
    old = _new_row(sid, name, _config())                # the max id
    db = SessionLocal()
    try:
        old_token = R.register_runner(db, old, identity=R.process_identity()
                                      if who == "same pid" else _dead_identity())
    finally:
        db.close()
    tid = H.checkpoint_task_id(name, old)
    H._save_checkpoint(tid, {"generation": 0, "partial": True, "population": [],
                             "optimization_id": old, "run_token": old_token})
    _delete_row_only(SO, old)
    new = _new_row(sid, name, _config())
    assert new == old, "precondition: SQLite reused the id"
    db = SessionLocal()
    try:
        token = R.register_runner(db, new)              # neither crashes nor inherits
        assert token != old_token
        assert R.checkpoint_owned_by(db, H._load_checkpoint(tid), [new]) is None
        assert db.get(Run, new).pid == os.getpid()
    finally:
        db.close()
    H._clear_checkpoint(tid)


def test_a_reused_backtest_id_never_inherits_the_deleted_rows_writer(delete_triggers):
    def make():
        db = SessionLocal()
        try:
            bt = Backtest(name=_uniq("WF1-OOS-R1-reuse"), engine_type="daily_expert",
                          status="running", initial_capital=1.0, start_date=datetime(2026, 1, 1),
                          end_date=datetime(2026, 6, 30))
            db.add(bt); db.flush()
            R.register_backtest_writer(db, bt.id, identity=R.process_identity())
            db.commit()
            return bt.id
        finally:
            db.close()
    old = make()
    _delete_row_only(Backtest, old)
    db = SessionLocal()
    try:
        bt = Backtest(name="WF1-OOS-R1-other", engine_type="daily_expert", status="running",
                      initial_capital=1.0, start_date=datetime(2026, 1, 1),
                      end_date=datetime(2026, 6, 30))
        db.add(bt); db.commit()
        assert bt.id == old
        state, why = R.judge_backtest_writer(db, bt.id)
        assert state == R.UNKNOWN and "no writer" in why          # not the old ALIVE writer
    finally:
        db.close()


# ------------------------------------------------------------------ 2. pause vs claim


def _pause_at(n):
    calls = []

    def stub(cfg, hoisted, decoded):
        calls.append(1)
        if len(calls) == n:
            raise InterruptedError("paused by the task queue")
        from .test_strategy_optimization_handler import _deterministic_stub
        return _deterministic_stub(cfg, hoisted, decoded)
    return stub


def test_a_paused_task_is_not_claimable_while_its_process_lives(stubbed):
    name = _uniq("paused")
    oid = _new_row(_seed_wide_strategy(), name, _config())
    H._run_trial_backtest = _pause_at(18)
    assert H.handle_strategy_optimization("t", {"optimization_id": oid})["status"] == "paused"
    db = SessionLocal()
    try:
        with pytest.raises(R.ResumeRefused, match="PAUSED"):
            R.assess(db, name)
        with pytest.raises(R.ResumeRefused, match="PAUSED"):
            R.plain_launch_gate(db, name)
    finally:
        db.close()


def test_a_paused_task_resumed_after_its_row_was_claimed_refuses_and_touches_nothing(stubbed):
    """The claim happened anyway (its serve process died and came back, or a forced claim): the
    queue then resumes the paused task in this process. It must refuse; the row stays retired."""
    name = _uniq("paused-claimed")
    oid = _new_row(_seed_wide_strategy(), name, _config())
    H._run_trial_backtest = _pause_at(18)
    assert H.handle_strategy_optimization("t", {"optimization_id": oid})["status"] == "paused"
    db = SessionLocal()
    try:                                   # the claim, as _create_resumed_row writes it
        succ = SO(strategy_id=db.get(SO, oid).strategy_id, name=name, fitness_metric="sharpe",
                  optimization_type="genetic", optimization_config=_config(), status="pending")
        db.add(succ); db.flush()
        R.register_runner(db, succ.id, resumed_from_id=oid, commit=False)
        R.claim_and_retire(db, oid, name, f"resumed as optimization #{succ.id}")
        db.commit()
    finally:
        db.close()
    calls = []
    out = _run(oid, calls)                 # task_queue.resume_task -> the handler, same process
    assert out["status"] == "failed" and "claimed by optimization" in out["error"] and calls == []
    db = SessionLocal()
    try:
        row = db.get(SO, oid)
        assert row.status == "failed" and row.name == f"{name}~interrupted-{oid}"
    finally:
        db.close()


def test_a_paused_task_whose_process_died_is_resumable(stubbed):
    name = _uniq("paused-dead")
    oid = _new_row(_seed_wide_strategy(), name, _config())
    H._run_trial_backtest = _pause_at(18)
    assert H.handle_strategy_optimization("t", {"optimization_id": oid})["status"] == "paused"
    _make_dead(oid)
    db = SessionLocal()
    try:
        assert R.runner_of(db, oid).ended_as == "paused"
        assert R.assess(db, name).kind == "resume"
    finally:
        db.close()


# ------------------------------------------------------------------ 3. the plain launch


def _opt_args(name, **over):
    kw = dict(expert="FMPSenateTraderWeight", strategy="S1", fitness="consistent_annual_return",
              universe="AAPL,MSFT", start="2020-01-01", end="2025-12-31", run_schedule="daily",
              run_schedule_day="monday", name=name, rerun=False, option_min_volume=0,
              gates_off=False, ohlcv_history_source="fmp", resume_interrupted=False,
              save_top=3, parallel=0, submit=False)
    kw.update(over)
    return argparse.Namespace(**kw)


def _reaches_build(mod, monkeypatch):
    def stop(*a, **kw):
        raise RuntimeError("reached the build")
    monkeypatch.setattr(mod, "_build_strategy", stop)


def test_a_plain_launch_claims_a_dead_running_row_instead_of_running_beside_it(stubbed, monkeypatch,
                                                                              capsys):
    name = _uniq("plain-dead")
    _kill(name, 18)
    mod = _launcher()
    _reaches_build(mod, monkeypatch)
    with pytest.raises(RuntimeError, match="reached the build"):
        mod._cmd_optimize(_opt_args(name))
    assert "RESUMING interrupted" in capsys.readouterr().out


def test_a_plain_launch_refuses_beside_a_live_row_and_a_submit_refuses_a_dead_one(stubbed):
    mod = _launcher()
    live = _uniq("plain-live")
    oid = _new_row(_seed_wide_strategy(), live, _config())
    db = SessionLocal()
    try:
        R.register_runner(db, oid)                        # this (live) process runs it
    finally:
        db.close()
    assert mod._cmd_optimize(_opt_args(live)) == 2
    dead = _uniq("plain-submit")
    _kill(dead, 18)
    assert mod._cmd_optimize(_opt_args(dead, submit=True)) == 2


def test_a_plain_launch_supersedes_a_pending_row_that_never_ran(stubbed, monkeypatch, capsys):
    name = _uniq("plain-shell")
    oid = _new_row(_seed_wide_strategy(), name, _config())
    db = SessionLocal()
    try:
        R.register_runner(db, oid, identity=_dead_identity())   # its launcher died first
    finally:
        db.close()
    mod = _launcher()
    _reaches_build(mod, monkeypatch)
    with pytest.raises(RuntimeError, match="reached the build"):
        mod._cmd_optimize(_opt_args(name))
    assert "never ran" in capsys.readouterr().out


# ------------------------------------------------------------------ 4. abandon


def test_abandon_retires_row_runner_and_checkpoint_and_states_the_cost(stubbed, capsys):
    name = _uniq("abandon")
    killed, done = _kill(name, 18)
    tid = H.checkpoint_task_id(name, killed)
    payload = H._load_checkpoint(tid)
    mod = _launcher()
    rc = mod._cmd_abandon_interrupted(argparse.Namespace(
        name=name, id=None, backtest_id=None, reason="test", confirm_not_running=False))
    out = capsys.readouterr().out
    assert rc == 0 and f"{done} finished trial(s) will NOT be reused" in out
    assert "trains from generation 0" in out
    db = SessionLocal()
    try:
        row = db.get(SO, killed)
        assert row.name == f"{name}~abandoned-{killed}" and row.status == "failed"
        assert "ABANDONED by operator (test)" in row.error_message
        assert R.runner_of(db, killed).ended_as == "abandoned"
        assert R.assess(db, name).kind == "none"
        from app.models.task_queue import TaskQueue
        assert H._load_checkpoint(tid) is None
        kept = db.query(TaskQueue.checkpoint_data).filter(
            TaskQueue.task_id.like(f"{tid}-r%")).all()
        assert [k[0] for k in kept] == [payload]                   # payload kept
    finally:
        db.close()


def test_abandon_refuses_a_live_run_and_needs_confirmation_for_an_unjudgeable_one(stubbed):
    live = _uniq("abandon-live")
    oid = _new_row(_seed_wide_strategy(), live, _config())
    legacy = _new_row(_seed_wide_strategy(), _uniq("abandon-legacy"), _config())
    db = SessionLocal()
    try:
        R.register_runner(db, oid)
        with pytest.raises(R.ResumeRefused, match="ALIVE"):
            R.abandon_optimization(db, oid, "x")
        with pytest.raises(R.ResumeRefused, match="--confirm-not-running"):
            R.abandon_optimization(db, legacy, "x")              # no runner record
        assert R.abandon_optimization(db, legacy, "x", confirm_not_running=True)["renamed"] \
            .endswith(f"~abandoned-{legacy}")
    finally:
        db.close()


def test_every_operator_message_states_the_cost_and_names_the_command(stubbed):
    name = _uniq("msg")
    oid = _new_row(_seed_wide_strategy(), name, _config())         # no runner: unjudgeable
    db = SessionLocal()
    try:
        db.get(SO, oid).status = "running"
        db.commit()
        with pytest.raises(R.ResumeRefused) as e:
            R.assess(db, name)
    finally:
        db.close()
    msg = str(e.value)
    assert "abandon-interrupted --id" in msg and "COSTS its finished trials" in msg
    assert "generation 0" in msg and "delete" not in msg.lower()


# ------------------------------------------------------------------ 5. --submit TOP-N marker


def test_a_handler_run_without_a_launcher_marks_its_topn_done(stubbed):
    for by_launcher in (False, True):
        oid = _new_row(_seed_wide_strategy(), _uniq("topn-mark"), _config())
        assert _run(oid, [], payload={"topn_by_launcher": True} if by_launcher else None)[
            "status"] == "completed"
        db = SessionLocal()
        try:
            assert (R.runner_of(db, oid).topn_done_at is None) == by_launcher
        finally:
            db.close()


# ------------------------------------------------------------------ 6. kill before the first trial


def _remote_only(monkeypatch):
    """Remote-only (0 local slots), as test_handler_completes_remote_only_zero_local_slots."""
    def fake_run_trial(worker, config, metric, **kw):
        return {"ok": True, "fitness": 9.0, "trades": 5, "error": None}
    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "push_secrets", lambda w, s, **k: {"set": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 3})
    monkeypatch.setattr(de.worker_client, "run_trial", fake_run_trial)
    db = SessionLocal()
    try:
        w = Worker(name=_uniq("remote-only"), url="http://x:8100", password="p",
                   worker_type="remote", is_local=False, is_enabled=True, status="offline")
        db.add(w); db.commit(); db.refresh(w)
        wid = w.id
    finally:
        db.close()
    cfg = _config(parallelIndividuals=0)
    cfg["backtest"].update(backtest_id=7, experts=[], enabled_instruments=["AAPL"],
                           account_settings={"starting_cash": 100000.0}, warmup_days=30,
                           initial_capital=100000.0)
    return cfg, wid


def _row_with_workers(name, cfg, wid):
    oid = _new_row(_seed_wide_strategy(), name, cfg)
    db = SessionLocal()
    try:
        db.get(SO, oid).worker_ids = [wid]
        db.commit()
    finally:
        db.close()
    return oid


def test_a_kill_while_a_remote_only_job_waits_for_its_workers_is_resumable(stubbed, monkeypatch):
    """The long wait before the first trial of a remote-only job is DistributedEvaluator.start
    (version check + cache push to every worker), after the runner is registered and the row is
    `running`. A kill there leaves no trial and no checkpoint: the next start claims the row and
    ends with the uninterrupted result."""
    cfg, wid = _remote_only(monkeypatch)
    whole_id = _row_with_workers(_uniq("remote-whole"), cfg, wid)
    assert H.handle_strategy_optimization("t", {"optimization_id": whole_id})["status"] == \
        "completed"
    want = _result(whole_id)

    name = _uniq("remote-cut")
    killed = _row_with_workers(name, cfg, wid)
    real_start = de.DistributedEvaluator.start

    def die(self):
        raise _Killed()
    monkeypatch.setattr(de.DistributedEvaluator, "start", die)
    with pytest.raises(_Killed):
        H.handle_strategy_optimization("t", {"optimization_id": killed})
    monkeypatch.setattr(de.DistributedEvaluator, "start", real_start)
    db = SessionLocal()
    try:
        row = db.get(SO, killed)
        assert row.status == "running" and not row.all_results
    finally:
        db.close()
    _make_dead(killed)
    L = _launcher()
    db = SessionLocal()
    try:
        assert R.assess(db, name).kind == "resume"
        from app.models.strategy import Strategy
        new = L._create_resumed_row(db, killed, name, cfg, db.get(Strategy, _seed_wide_strategy()),
                                    "sharpe", [wid]).id
    finally:
        db.close()
    out = H.handle_strategy_optimization("t", {"optimization_id": new, "resume_of": killed})
    assert out["status"] == "completed", out
    assert _result(new) == {**want, "status": "completed"}
