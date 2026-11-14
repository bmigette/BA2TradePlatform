"""Walk-forward driver side of resuming an interrupted grid (tools/run_walk_forward.py RealRunner,
wf_chain.run_cell, the launcher's --resume-interrupted gate and TOP-N completion).

What a ``grid_goal2027.sh stop`` can interrupt, and what the next start must do:
  * a fold's TRAINING job -> relaunched with --resume-interrupted when its runner is provably dead;
    refused when it is live, failed, ownerless or on another host;
  * the launcher's end-of-job TOP-N persist -> the job is completed, the missing TOP ranks are
    re-run (a half-written row retired, never duplicated);
  * an OUT-OF-SAMPLE re-run's result write -> under the driver's cell lock the half-written row is
    retired and the genome re-run;
  * a report write between folds -> written atomically, never left truncated.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime

import pytest

from app.services import optimization_resume as R
from app.services import walk_forward as WF
from app.services import wf_chain as C

from .test_walk_forward import _pick
from .test_walk_forward_followups import _fold, _real_runner, _seed_bt, _seed_opt, host_db  # noqa: F401
from .test_wf_matrix_chain import GOOD, ChainRunner, _spec


def _dead():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    ident = R.process_identity(p.pid)
    p.wait()
    return ident


def _writer(S, bt_id, identity):
    db = S()
    try:
        R.register_backtest_writer(db, bt_id, identity=identity)
        db.commit()
    finally:
        db.close()


def _register(S, opt_id, identity, topn_done=False):
    db = S()
    try:
        R.register_runner(db, opt_id, identity=identity, versions=R.code_versions())
        if topn_done:
            R.mark_topn_done(db, opt_id)
    finally:
        db.close()


@pytest.fixture
def launched(monkeypatch, host_db):
    """A RealRunner whose train subprocess is recorded instead of run. Patched in the runner's
    OWN module globals only: a global subprocess.run patch would also catch git."""
    import types
    calls, rc = [], {}

    def run(cmd, env=None):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, rc.get("rc", 0))
    r, _, _ = _real_runner(monkeypatch, host_db)
    monkeypatch.setitem(type(r).run_train.__globals__, "subprocess",
                        types.SimpleNamespace(run=run))
    return r, calls, rc


# ------------------------------------------------------------------ the training job


def test_a_killed_training_job_is_relaunched_with_resume(monkeypatch, host_db, launched):
    r, calls, _ = launched
    oid = _seed_opt(host_db, "rs-cell-wf2", status="running")
    _register(host_db, oid, _dead())
    assert r.train_completed("rs-cell-wf2") is False
    r.run_train(["ba2-test", "optimize", "--name", "rs-cell-wf2"], "rs-cell-wf2")
    assert calls == [["ba2-test", "optimize", "--name", "rs-cell-wf2", "--resume-interrupted"]]


def test_a_live_training_job_is_never_hijacked(monkeypatch, host_db, launched):
    r, calls, _ = launched
    oid = _seed_opt(host_db, "rs-live-wf1", status="running")
    _register(host_db, oid, R.process_identity())
    with pytest.raises(WF.WalkForwardError, match="LIVE process"):
        r.run_train(["ba2-test", "optimize"], "rs-live-wf1")
    assert calls == []


def test_a_fresh_job_is_launched_without_the_flag(monkeypatch, host_db, launched):
    r, calls, _ = launched
    r.run_train(["ba2-test", "optimize"], "rs-fresh-wf1")
    assert calls == [["ba2-test", "optimize"]]


def test_a_refusal_by_the_launcher_is_named(monkeypatch, host_db, launched):
    r, _, rc = launched
    rc["rc"] = 2
    with pytest.raises(WF.WalkForwardError, match="code 2 .refused"):
        r.run_train(["ba2-test", "optimize"], "rs-refused-wf1")


def test_a_cut_off_topn_persist_reopens_a_completed_job(monkeypatch, host_db, launched):
    r, calls, _ = launched
    oid = _seed_opt(host_db, "rs-topn-wf1", status="completed")
    _register(host_db, oid, _dead())
    assert r.train_completed("rs-topn-wf1") is False
    r.run_train(["ba2-test", "optimize"], "rs-topn-wf1")
    assert calls == [["ba2-test", "optimize", "--resume-interrupted"]]
    _register(host_db, oid, _dead(), topn_done=True)
    assert r.train_completed("rs-topn-wf1") is True


def test_the_engine_owns_the_resume_flag():
    with pytest.raises(WF.WalkForwardError, match="must not contain --resume-interrupted"):
        WF.check_user_optimize_args(["--expert", "X", "--resume-interrupted"])


# ------------------------------------------------------------------ out-of-sample re-runs


class _OosLauncher:
    def __init__(self, S, job):
        self.S, self.job, self.calls = S, job, []

    def _persist_top_backtests(self, opt_id, expert, **kw):
        self.calls.append([p for p in kw["ranks"]])
        for (params, key, fit), rank in zip(kw["candidates"], kw["ranks"]):
            bid = _seed_bt(self.S, opt_id, f"{kw['name_prefix']}{rank}-{self.job}",
                           datetime.fromisoformat(kw["window"][0]),
                           datetime.fromisoformat(kw["window"][1]), ga_fitness=fit, ret=3.0)
            kw["persisted_ids"].append((rank, bid))
        return len(kw["candidates"])


def test_a_half_written_oos_row_is_retired_and_rerun_under_the_cell_lock(monkeypatch, host_db,
                                                                         tmp_path):
    r, _, _ = _real_runner(monkeypatch, host_db)
    job = "rs-oos-wf1"
    oid = _seed_opt(host_db, job)
    r._opt[job] = (oid, "FMPRating")
    fake = _OosLauncher(host_db, job)
    r._launcher = fake
    half = _seed_bt(host_db, oid, f"WF1-OOS-R1-{job}", datetime(2026, 1, 1), datetime(2026, 6, 30),
                    status="running")
    _writer(host_db, half, R.process_identity())            # its writer is still alive
    # without the lock: refused as before (another driver may be writing it)
    with pytest.raises(WF.WalkForwardError, match="running"):
        r.run_oos(job, _fold(), [_pick(1)], "WF1-OOS-R", ["OOS"], 1)
    # with the lock but a LIVE writer (an orphaned persist after only the driver was killed)
    with r.exclusive("rs-oos", str(tmp_path / ".lock")):
        with pytest.raises(WF.WalkForwardError, match="writer is ALIVE"):
            r.run_oos(job, _fold(), [_pick(1)], "WF1-OOS-R", ["OOS"], 1)
    assert fake.calls == []
    _writer(host_db, half, _dead())
    with r.exclusive("rs-oos", str(tmp_path / ".lock")):
        out = r.run_oos(job, _fold(), [_pick(1)], "WF1-OOS-R", ["OOS"], 1)
    assert fake.calls == [[1]] and not out[1].reused and out[1].total_return == 3.0
    from app.models.backtest import Backtest
    db = host_db()
    try:
        old = db.get(Backtest, half)
        assert old.name == f"WF1-OOS-R1-{job}~interrupted-{half}" and old.status == "failed"
        same = db.query(Backtest).filter(Backtest.name == f"WF1-OOS-R1-{job}").all()
        assert [b.status for b in same] == ["completed"]            # exactly one, no duplicate
    finally:
        db.close()
    with R.exclusive(str(tmp_path / ".lock"), "rs-oos"):        # released
        pass


# ------------------------------------------------------------------ the cell lock


class _LockingRunner(ChainRunner):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.locked = []

    @contextmanager
    def exclusive(self, prefix, path):
        with R.exclusive(path, prefix):
            self.locked.append(path)
            yield


def test_a_cell_runs_under_its_lock_and_a_second_driver_is_refused(tmp_path):
    r = _LockingRunner([GOOD, GOOD, GOOD])
    res = C.run_cell(_spec(), r, str(tmp_path), log=lambda *_: None)
    assert res.verdict == "PASS"
    assert r.locked == [C.lock_path(str(tmp_path), "cell")]
    with R.exclusive(C.lock_path(str(tmp_path), "cell"), "cell"):   # released after the cell
        pass
    # a live driver holds the cell: the next one records ERROR for it and runs nothing
    with R.exclusive(C.lock_path(str(tmp_path), "cell"), "cell"):
        again = _LockingRunner([GOOD, GOOD, GOOD])
        cells = C.run_cells([_spec()], lambda _s: again, str(tmp_path), log=lambda *_: None)
    assert cells[0]["verdict"] == "ERROR" and "LIVE process" in cells[0]["error"]
    assert again.events == []


def test_reports_are_written_atomically(tmp_path, monkeypatch):
    path = tmp_path / "cell" / "fold1.json"
    WF.atomic_write_text(str(path), '{"old": true}')

    def boom(src, dst):
        raise OSError("killed mid-write")
    monkeypatch.setattr(WF.os, "replace", boom)
    with pytest.raises(OSError):
        WF.atomic_write_text(str(path), '{"new": tru')
    assert json.loads(path.read_text()) == {"old": True}
    assert [p.name for p in path.parent.iterdir()] == ["fold1.json"]


# ------------------------------------------------------------------ launcher


def _launcher_module():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "ba2test_launcher_resume", os.path.normpath(os.path.join(root, "..", "ba2test_launcher.py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _opt_args(name, **over):
    kw = dict(expert="FMPSenateTraderWeight", strategy="S1", fitness="consistent_annual_return",
              universe="AAPL,MSFT", start="2020-01-01", end="2025-12-31", run_schedule="daily",
              run_schedule_day="monday", name=name, rerun=False, option_min_volume=0,
              gates_off=False, ohlcv_history_source="fmp", resume_interrupted=True,
              save_top=3, parallel=0)
    kw.update(over)
    return argparse.Namespace(**kw)


def test_launcher_refuses_to_resume_a_live_job_and_creates_nothing(host_db):
    mod = _launcher_module()
    oid = _seed_opt(host_db, "rs-l-live", status="running")
    _register(host_db, oid, R.process_identity())
    assert mod._cmd_optimize(_opt_args("rs-l-live")) == 2
    db = host_db()
    try:
        from app.models.strategy_optimization import StrategyOptimization as SO
        assert db.query(SO).filter(SO.name == "rs-l-live").count() == 1
    finally:
        db.close()


def test_launcher_lets_a_dead_job_through_to_the_build(host_db, monkeypatch):
    mod = _launcher_module()
    oid = _seed_opt(host_db, "rs-l-dead", status="running")
    _register(host_db, oid, _dead())

    def stop(*a, **kw):
        raise RuntimeError("reached the build")
    monkeypatch.setattr(mod, "_build_strategy", stop)
    with pytest.raises(RuntimeError, match="reached the build"):
        mod._cmd_optimize(_opt_args("rs-l-dead"))


def test_launcher_completes_a_cut_off_topn_persist(host_db, monkeypatch):
    from app.models.backtest import Backtest
    from app.models.strategy_optimization import StrategyOptimization as SO
    from app.models.strategy_optimization import StrategyOptimizationRunner as Run
    mod = _launcher_module()
    oid = _seed_opt(host_db, "rs-l-topn", status="completed")
    db = host_db()
    try:
        row = db.get(SO, oid)
        row.all_results = [{"params": {"g": i}, "key": f"k{i}", "fitness": float(i), "trades": 5}
                           for i in (3, 2, 1)]
        db.commit()
    finally:
        db.close()
    _register(host_db, oid, _dead())
    _seed_bt(host_db, oid, "TOP1-rs-l-topn", datetime(2020, 1, 1), datetime(2025, 12, 31))
    half = _seed_bt(host_db, oid, "TOP2-rs-l-topn", datetime(2020, 1, 1), datetime(2025, 12, 31),
                    status="running")
    seen = []
    monkeypatch.setattr(mod, "_persist_top_backtests",
                        lambda opt_id, expert, **kw: seen.append((opt_id, kw["ranks"],
                                                                  [c[2] for c in kw["candidates"]])))
    _writer(host_db, half, R.process_identity())              # still being written: hands off
    assert mod._cmd_optimize(_opt_args("rs-l-topn")) == 2 and seen == []
    _writer(host_db, half, _dead())
    assert mod._cmd_optimize(_opt_args("rs-l-topn")) == 0
    assert seen == [(oid, [2, 3], [2.0, 1.0])]
    db = host_db()
    try:
        assert db.get(Backtest, half).status == "failed"
        assert db.get(Run, oid).topn_done_at is not None
    finally:
        db.close()


def test_topn_completion_persists_exactly_what_the_uninterrupted_persist_would(host_db,
                                                                              monkeypatch):
    """Same ranking as the end-of-job persist (``_persist_top_backtests`` with no candidates ranks
    ``_rank_measured_candidates(all_results, save_top, best_params, best_fitness)`` as 1..n):
    with stalled records and fitness ties in all_results, the completion re-runs ranks 2..n with
    the very candidates the uninterrupted persist would have given those ranks."""
    from app.models.strategy_optimization import StrategyOptimization as SO
    mod = _launcher_module()
    recs = ([{"params": {"g": i}, "key": f"k{i}", "fitness": f, "trades": 5}
             for i, f in enumerate([5.0, 4.0, 4.0, 3.5, 3.0, 2.0])]
            + [{"params": {"g": 99}, "key": "kst", "fitness": -1e9, "trades": 0,
                "status": "stalled"}])
    oid = _seed_opt(host_db, "rs-l-rank", status="completed")
    db = host_db()
    try:
        row = db.get(SO, oid)
        row.all_results, row.best_params, row.best_fitness = recs, {"g": 0}, 5.0
        db.commit()
    finally:
        db.close()
    _register(host_db, oid, _dead())
    _seed_bt(host_db, oid, "TOP1-rs-l-rank", datetime(2020, 1, 1), datetime(2025, 12, 31))
    uninterrupted, _ = mod._rank_measured_candidates(recs, 4, {"g": 0}, 5.0)
    seen = []
    monkeypatch.setattr(mod, "_persist_top_backtests",
                        lambda opt_id, expert, **kw: seen.append((kw["ranks"], kw["candidates"],
                                                                  kw.get("name_prefix", "TOP"))))
    assert mod._cmd_optimize(_opt_args("rs-l-rank", save_top=4)) == 0
    assert seen == [([2, 3, 4], list(uninterrupted[1:]), "TOP")]
    assert len(uninterrupted) == 4 and all(c[2] > -1e8 for c in uninterrupted)   # stall excluded
