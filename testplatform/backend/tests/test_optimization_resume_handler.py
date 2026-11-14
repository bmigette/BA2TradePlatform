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

import importlib.util
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
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    ident = R.process_identity(p.pid)
    p.wait()
    db = SessionLocal()
    try:
        run = db.get(StrategyOptimizationRunner, opt_id)
        run.pid, run.pid_started, run.proc_identity = ident["pid"], ident["pid_started"], \
            ident["proc_identity"]
        db.commit()
    finally:
        db.close()


def _config(**over):
    cfg = _ga_config(populationSize=12, generations=8, earlyStoppingGenerations=99,
                     mutationProb=0.6, crossoverProb=0.9)          # 30 trials, 12 in generation 0
    cfg.update(over)
    return cfg


_LAUNCHER = {}


def _launcher():
    if "m" not in _LAUNCHER:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "ba2test_launcher_resume_h", os.path.normpath(os.path.join(root, "..", "ba2test_launcher.py")))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _LAUNCHER["m"] = m
    return _LAUNCHER["m"]


def _run(opt_id, calls, kill_at=None, payload=None):
    H._run_trial_backtest = _counting_stub(calls, kill_at)
    return H.handle_strategy_optimization("t", {"optimization_id": opt_id, **(payload or {})})


def _kill(name, kill_at, config=None):
    """Run job ``name`` until trial ``kill_at`` kills it; its runner is then dead."""
    calls = []
    killed = _new_row(_seed_wide_strategy(), name, config or _config())
    with pytest.raises(_Killed):
        _run(killed, calls, kill_at)
    db = SessionLocal()
    try:
        assert db.get(StrategyOptimization, killed).status == "running"
        assert db.get(StrategyOptimizationRunner, killed).pid == os.getpid()
    finally:
        db.close()
    _make_dead(killed)
    return killed, len(calls) - 1


def _launch_resume(name, config=None):
    """What `optimize --resume-interrupted` does once its config is built: assess, then the
    launcher's own _create_resumed_row (identity check + atomic claim and insert). Returns
    (stale id, new row id)."""
    L = _launcher()
    db = SessionLocal()
    try:
        a = R.assess(db, name)
        assert a.kind == "resume", a
        from app.models.strategy import Strategy
        strat = db.get(Strategy, _seed_wide_strategy())       # the launcher builds a fresh row
        opt = L._create_resumed_row(db, a.opt_id, name, config or _config(), strat, "sharpe", None)
        return a.opt_id, opt.id
    finally:
        db.close()


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    monkeypatch.setattr(H, "_run_trial_backtest", H._run_trial_backtest)   # restored after


@pytest.fixture
def whole(stubbed):
    """The uninterrupted job: (result, trials)."""
    calls = []
    oid = _new_row(_seed_wide_strategy(), f"res-whole-{os.getpid()}-{len(_LAUNCHER)}", _config())
    assert _run(oid, calls)["status"] == "completed"
    return _result(oid), len(calls)


def _uniq(stem):
    import uuid
    return f"{stem}-{uuid.uuid4().hex[:8]}"


@pytest.mark.parametrize("kill_at", [3, 18, 27])      # generation 0, mid-run, late
def test_resumed_job_equals_the_uninterrupted_job(whole, kill_at):
    want, n_whole = whole
    name = _uniq("res-cut")
    killed, done_before_kill = _kill(name, kill_at)
    stale, resumed = _launch_resume(name)
    assert stale == killed
    calls = []
    assert _run(resumed, calls, payload={"resume_of": killed})["status"] == "completed"
    assert _result(resumed) == {**want, "status": "completed"}
    assert done_before_kill + len(calls) == n_whole      # nothing that finished ran again
    db = SessionLocal()
    try:
        old = db.get(StrategyOptimization, killed)
        assert old.name == f"{name}~interrupted-{killed}" and old.status == "failed"
        assert f"resumed as optimization #{resumed}" in old.error_message
        assert db.get(StrategyOptimizationRunner, resumed).resumed_from_id == killed
        rf = db.get(StrategyOptimization, resumed).optimization_config["resumedFrom"]
        assert rf["optimization_id"] == killed
        assert rf["code_versions_before"]["app_version"] and rf["code_versions_now"]["app_version"]
    finally:
        db.close()


def test_the_launcher_refuses_a_different_job_and_changes_nothing(stubbed):
    name = _uniq("res-diff")
    killed, _ = _kill(name, 18)
    with pytest.raises(R.ResumeRefused, match="generations: 8 -> 9"):
        _launch_resume(name, _config(generations=9))
    db = SessionLocal()
    try:
        rows = db.query(StrategyOptimization.id, StrategyOptimization.status).filter(
            StrategyOptimization.name == name).all()
        assert rows == [(killed, "running")]
    finally:
        db.close()


def test_a_second_starter_loses_the_claim(stubbed):
    """Both starters assessed the dead row; the second claim (after the first one committed)
    matches no row: refused, and no second row is created."""
    name = _uniq("res-race")
    killed, _ = _kill(name, 18)
    L = _launcher()
    from app.models.strategy import Strategy
    a, b = SessionLocal(), SessionLocal()
    try:
        assert R.assess(a, name).kind == "resume" and R.assess(b, name).kind == "resume"
        L._create_resumed_row(a, killed, name, _config(), a.get(Strategy, _seed_wide_strategy()),
                              "sharpe", None)
        with pytest.raises(R.ResumeRefused, match="claimed by another starter"):
            L._create_resumed_row(b, killed, name, _config(),
                                  b.get(Strategy, _seed_wide_strategy()), "sharpe", None)
        assert len(a.query(StrategyOptimization.id).filter(StrategyOptimization.name == name)
                   .all()) == 1
    finally:
        a.close()
        b.close()


def _tamper(name, killed, **changes):
    tid = H.checkpoint_task_id(name, killed)
    ckpt = H._load_checkpoint(tid)
    assert ckpt is not None
    ckpt.update(changes)
    for k in [k for k, v in changes.items() if v is None]:
        del ckpt[k]
    H._save_checkpoint(tid, ckpt)


@pytest.mark.parametrize("change, error", [
    ({"fingerprint": "another-space"}, "another search"),
    ({"fingerprint": None}, "no fingerprint"),
    ({"job_identity": "another-window"}, "job identity"),
])
def test_a_resume_refuses_its_own_checkpoint_of_another_search(stubbed, change, error):
    """The plain path discards a checkpoint of another search and starts over; a resume that was
    asked for fails loudly instead."""
    name = _uniq("res-ck")
    killed, _ = _kill(name, 18)
    _tamper(name, killed, **change)
    _, resumed = _launch_resume(name)
    out = _run(resumed, [], payload={"resume_of": killed})
    assert out["status"] == "failed" and error in out["error"], out


def test_an_exhausted_checkpoint_is_replayed_to_the_same_result(whole, monkeypatch):
    """Killed after the last generation's checkpoint, before completing: the resume replays the
    search from the memo, runs no trial and ends where the uninterrupted job ended."""
    want, n_whole = whole
    name = _uniq("res-exh")
    real_clear = H._clear_checkpoint

    def die(task_id):
        raise _Killed()
    monkeypatch.setattr(H, "_clear_checkpoint", die)
    calls = []
    killed = _new_row(_seed_wide_strategy(), name, _config())
    with pytest.raises(_Killed):
        _run(killed, calls)
    monkeypatch.setattr(H, "_clear_checkpoint", real_clear)
    assert len(calls) == n_whole
    _make_dead(killed)
    _, resumed = _launch_resume(name)
    after = []
    assert _run(resumed, after, payload={"resume_of": killed})["status"] == "completed"
    assert after == [] and _result(resumed) == {**want, "status": "completed"}


def test_a_foreign_checkpoint_is_discarded_and_the_resume_starts_clean(whole, caplog):
    """Job killed BEFORE its first checkpoint while an older run's checkpoint of the same name is
    still stored: that checkpoint is never used (said so), the run starts clean."""
    want, n_whole = whole
    name = _uniq("res-foreign")
    older, _ = _kill(name, 18)                       # wrote a checkpoint (end of generation 0)
    older_ckpt = H._load_checkpoint(H.checkpoint_task_id(name, older))
    db = SessionLocal()
    try:
        db.delete(db.get(StrategyOptimizationRunner, older))
        db.delete(db.get(StrategyOptimization, older))            # deleted by hand
        db.commit()
    finally:
        db.close()
    killed, done = _kill(name, 3)                    # the plain start discarded it ...
    H._save_checkpoint(H.checkpoint_task_id(name, killed), older_ckpt)   # ... say it lingered
    _, resumed = _launch_resume(name)
    calls = []
    caplog.set_level("WARNING")
    assert _run(resumed, calls, payload={"resume_of": killed})["status"] == "completed"
    assert "DISCARDING checkpoint" in caplog.text and f"optimization {older}" in caplog.text
    assert _result(resumed) == {**want, "status": "completed"}
    assert done + len(calls) == n_whole


def test_a_fresh_start_discards_an_orphaned_checkpoint(whole, caplog):
    """Production: rows deleted by hand, checkpoints left in task_queue. A fresh launch of the name
    must not resume such a checkpoint with only its 20 elites in all_results: it is discarded
    (said so) and the job trains from generation 0 to the full uninterrupted result."""
    want, n_whole = whole
    name = _uniq("res-orphan")
    older, _ = _kill(name, 18)
    db = SessionLocal()
    try:
        db.delete(db.get(StrategyOptimizationRunner, older))
        db.delete(db.get(StrategyOptimization, older))            # deleted by hand
        db.commit()
    finally:
        db.close()
    assert H._load_checkpoint(H.checkpoint_task_id(name, older)) is not None
    fresh = _new_row(_seed_wide_strategy(), name, _config())
    calls = []
    caplog.set_level("WARNING")
    assert _run(fresh, calls)["status"] == "completed"
    assert "DISCARDING checkpoint" in caplog.text and "is orphaned" in caplog.text
    assert _result(fresh) == {**want, "status": "completed"} and len(calls) == n_whole


def test_a_start_killed_right_after_its_claim_is_itself_resumable(whole):
    """The claim committed (dead row retired, new row `pending` with its runner and the link to
    the row it continues), then the process died before the handler ran: the next start resumes
    the pending row, finds the first row's checkpoint through the lineage and its trials through
    the newest row holding any, and ends with the uninterrupted result."""
    want, n_whole = whole
    name = _uniq("res-claimkill")
    killed, done = _kill(name, 18)
    _, pending = _launch_resume(name)            # claimed ...
    _make_dead(pending)                          # ... and killed before the handler started
    db = SessionLocal()
    try:
        assert db.get(StrategyOptimization, pending).status == "pending"
        assert db.get(StrategyOptimizationRunner, pending).resumed_from_id == killed
        a = R.assess(db, name)
        assert (a.kind, a.opt_id) == ("resume", pending)
    finally:
        db.close()
    stale, resumed = _launch_resume(name)
    assert stale == pending
    calls = []
    caplog_free = _run(resumed, calls, payload={"resume_of": pending})
    assert caplog_free["status"] == "completed", caplog_free
    assert _result(resumed) == {**want, "status": "completed"}
    assert done + len(calls) == n_whole                # resumed, nothing re-run


def test_the_handler_leaves_a_row_owned_by_another_process_untouched(stubbed):
    name = _uniq("res-owned")
    killed, _ = _kill(name, 18)                  # its runner: another (dead) process
    db = SessionLocal()
    try:
        before = db.get(StrategyOptimizationRunner, killed).run_token
    finally:
        db.close()
    out = _run(killed, [])                       # a plain re-run of THAT row by this process
    assert out["status"] == "failed" and "--resume-interrupted" in out["error"]
    db = SessionLocal()
    try:
        row = db.get(StrategyOptimization, killed)
        assert row.status == "running" and row.name == name          # not touched
        assert db.get(StrategyOptimizationRunner, killed).run_token == before
    finally:
        db.close()
