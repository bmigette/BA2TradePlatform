"""Resume of an interrupted optimization (app.services.optimization_resume).

The grid runs as a systemd unit and `grid_goal2027.sh stop` kills the whole cgroup, so a fold's
training job is left in the DB as ``running``. These tests pin the rules that decide what the next
start does with such a row: liveness from the recorded runner (never hijack a live job), the
refusals (failed, ownerless, foreign host, duplicates), the identity check (gene space, config,
data digests, code versions) and the retirement of the dead row that frees the job name.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from app.services import optimization_resume as R


@pytest.fixture
def S():
    import app.models  # noqa: F401
    from app.models.database import Base, SessionLocal, engine
    Base.metadata.create_all(bind=engine)
    return SessionLocal


_N = {"i": 0}


def _name(stem):
    _N["i"] += 1
    return f"{stem}-{os.getpid()}-{_N['i']}"


def _cfg(**over):
    cfg = {"populationSize": 8, "generations": 4, "seed": 42, "parallelIndividuals": 2,
           "expert_params": {"x": {"min": 0, "max": 5, "step": 1, "type": "int"}},
           "backtest": {"backtest_id": 111, "start_date": "2020-01-01", "end_date": "2023-12-31",
                        "market_condition_manifests": {"ohlcv-v1": "sha-A"},
                        "enabled_instruments": ["AAPL", "MSFT"]}}
    cfg.update(over)
    return cfg


def _opt(S, name, status="running", cfg=None, ranges=None, rules=None):
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    db = S()
    try:
        st = Strategy(name=f"s-{name}", entry_rules=rules if rules is not None else [{"id": "r"}],
                      exit_rules=[])
        db.add(st); db.commit(); db.refresh(st)
        o = StrategyOptimization(strategy_id=st.id, name=name, fitness_metric="sharpe",
                                 optimization_type="genetic",
                                 optimization_config=cfg if cfg is not None else _cfg(),
                                 parameter_ranges=ranges, status=status,
                                 all_results=[{"key": "k1", "fitness": 1.0}])
        db.add(o); db.commit(); db.refresh(o)
        return o.id
    finally:
        db.close()


def _dead_identity():
    """A process that existed and is gone: a finished child's pid + its real creation time."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    import psutil
    started = psutil.Process(p.pid).create_time()
    p.wait()
    return {"host": R.this_host(), "pid": p.pid, "pid_started": started}


def _register(S, opt_id, identity, versions=None, topn_done=False):
    db = S()
    try:
        R.register_runner(db, opt_id, identity=identity,
                          versions=versions if versions is not None else R.code_versions())
        if topn_done:
            R.mark_topn_done(db, opt_id)
    finally:
        db.close()


# ------------------------------------------------------------------ liveness


def test_this_process_is_alive():
    me = R.process_identity()
    assert R.process_alive(me["host"], me["pid"], me["pid_started"]) is True


def test_a_finished_process_is_dead():
    d = _dead_identity()
    assert R.process_alive(d["host"], d["pid"], d["pid_started"]) is False


def test_a_reused_pid_is_dead():
    """The pid exists but belongs to a process created at another time: not our runner."""
    me = R.process_identity()
    assert R.process_alive(me["host"], me["pid"], me["pid_started"] - 3600.0) is False


def test_another_host_is_unknown_never_dead():
    me = R.process_identity()
    assert R.process_alive("some-other-box", me["pid"], me["pid_started"]) is None


# ------------------------------------------------------------------ assess


def test_no_row_is_none(S):
    db = S()
    try:
        assert R.assess(db, _name("absent")).kind == "none"
    finally:
        db.close()


def test_completed_row_without_runner_is_done(S):
    n = _name("done")
    _opt(S, n, status="completed")
    db = S()
    try:
        assert R.assess(db, n).kind == "done"
    finally:
        db.close()


def test_completed_row_whose_topn_was_cut_is_topn(S):
    n = _name("topn")
    oid = _opt(S, n, status="completed")
    _register(S, oid, _dead_identity())
    db = S()
    try:
        a = R.assess(db, n)
        assert (a.kind, a.opt_id) == ("topn", oid)
    finally:
        db.close()
    _register(S, oid, _dead_identity(), topn_done=True)
    db = S()
    try:
        assert R.assess(db, n).kind == "done"
    finally:
        db.close()


def test_running_row_of_a_dead_runner_is_resumable(S):
    n = _name("dead")
    oid = _opt(S, n)
    _register(S, oid, _dead_identity())
    db = S()
    try:
        a = R.assess(db, n)
        assert (a.kind, a.opt_id) == ("resume", oid)
    finally:
        db.close()


def test_running_row_of_a_live_runner_is_refused(S):
    n = _name("live")
    oid = _opt(S, n)
    _register(S, oid, R.process_identity())
    db = S()
    try:
        with pytest.raises(R.ResumeRefused, match="LIVE process"):
            R.assess(db, n)
    finally:
        db.close()


def test_running_row_without_a_recorded_runner_is_refused(S):
    n = _name("legacy")
    oid = _opt(S, n)
    db = S()
    try:
        with pytest.raises(R.ResumeRefused, match=f"id={oid}.*no runner was recorded"):
            R.assess(db, n)
    finally:
        db.close()


def test_running_row_of_another_host_is_refused(S):
    n = _name("foreign")
    oid = _opt(S, n)
    _register(S, oid, {"host": "remote-box", "pid": 1, "pid_started": 1.0})
    db = S()
    try:
        with pytest.raises(R.ResumeRefused, match="remote-box"):
            R.assess(db, n)
    finally:
        db.close()


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_or_cancelled_row_is_refused(S, status):
    n = _name(status)
    _opt(S, n, status=status)
    db = S()
    try:
        with pytest.raises(R.ResumeRefused, match=status):
            R.assess(db, n)
    finally:
        db.close()


def test_two_rows_of_one_name_are_refused(S):
    n = _name("dup")
    _opt(S, n, status="completed")
    _opt(S, n, status="running")
    db = S()
    try:
        with pytest.raises(R.ResumeRefused, match="2 optimizations are named"):
            R.assess(db, n)
    finally:
        db.close()


# ------------------------------------------------------------------ identity


def _diffs(S, oid, **over):
    kw = dict(new_cfg=_cfg(), fitness_metric="sharpe", optimization_type="genetic",
              param_space={"x": {"min": 0, "max": 5, "step": 1, "type": "int"}},
              entry_rules=[{"id": "r"}], exit_rules=[], versions=R.code_versions())
    kw.update(over)
    db = S()
    try:
        return R.identity_differences(db, oid, **kw)
    finally:
        db.close()


def _stale(S, **kw):
    oid = _opt(S, _name("ident"), ranges={"x": {"min": 0, "max": 5, "step": 1, "type": "int"}},
               **kw)
    _register(S, oid, _dead_identity())
    return oid


def test_identical_job_has_no_differences_and_execution_keys_are_exempt(S):
    oid = _stale(S)
    new = _cfg(parallelIndividuals=16)            # local slots: execution only
    new["backtest"]["backtest_id"] = 999          # a launch timestamp
    assert _diffs(S, oid, new_cfg=new) == []


def test_seeded_from_written_by_the_handler_is_exempt(S):
    oid = _stale(S, cfg={**_cfg(), "seededFrom": {"name": "x-wf1"}})
    assert _diffs(S, oid) == []


def test_a_changed_data_digest_is_a_difference(S):
    oid = _stale(S)
    new = _cfg()
    new["backtest"]["market_condition_manifests"] = {"ohlcv-v1": "sha-B"}
    d = _diffs(S, oid, new_cfg=new)
    assert len(d) == 1 and "backtest.market_condition_manifests.ohlcv-v1" in d[0]
    assert "sha-A" in d[0] and "sha-B" in d[0]


def test_a_changed_gene_space_is_a_difference(S):
    oid = _stale(S)
    d = _diffs(S, oid, param_space={"x": {"min": 0, "max": 9, "step": 1, "type": "int"}})
    assert any("gene space" in x for x in d)


def test_changed_strategy_rules_are_a_difference(S):
    oid = _stale(S)
    assert any("entry_rules" in x for x in _diffs(S, oid, entry_rules=[{"id": "other"}]))


def test_a_changed_generation_budget_or_metric_is_a_difference(S):
    oid = _stale(S)
    assert any("generations" in x for x in _diffs(S, oid, new_cfg=_cfg(generations=9)))
    assert any("fitness_metric" in x for x in _diffs(S, oid, fitness_metric="return"))


def test_a_code_version_change_is_a_difference_unless_allowed(S):
    oid = _stale(S)
    other = {**R.code_versions(), "app_version": "1999.01.0001"}
    d = _diffs(S, oid, versions=other)
    assert len(d) == 1 and "app_version" in d[0] and R.ACROSS_CODE_CHANGE_ENV in d[0]
    assert _diffs(S, oid, versions=other, allow_code_change=True) == []
    raised = {**R.code_versions(), "required_package_versions": {"ba2_common": "2099.01.00001"}}
    assert any("required_package_versions" in x for x in _diffs(S, oid, versions=raised))


# ------------------------------------------------------------------ retire


def test_retire_frees_the_name_and_keeps_the_row(S):
    from app.models.strategy_optimization import StrategyOptimization as SO
    n = _name("ret")
    oid = _opt(S, n)
    db = S()
    try:
        new_name = R.retire_optimization(db, oid, "resumed as #77")
        row = db.get(SO, oid)
        assert row.name == new_name == f"{n}~interrupted-{oid}"
        assert row.status == "failed" and "INTERRUPTED" in row.error_message
        assert "resumed as #77" in row.error_message
        assert R.assess(db, n).kind == "none"
        assert row.all_results == [{"key": "k1", "fitness": 1.0}]       # evidence kept
    finally:
        db.close()


# ------------------------------------------------------------------ the driver's lock


def test_lock_is_exclusive_against_a_live_holder_and_taken_from_a_dead_one(tmp_path):
    p = str(tmp_path / "cell" / ".lock")
    with R.exclusive(p, "cell-a"):
        held = json.loads(open(p, encoding="utf-8").read())
        assert held["pid"] == os.getpid() and held["label"] == "cell-a"
        # a second driver while the holder (this process) lives: refused
        with pytest.raises(R.ResumeRefused, match="LIVE process"):
            R.acquire_lock(p, "cell-a")
    assert not os.path.exists(p)                    # released
    with open(p, "w", encoding="utf-8") as fh:      # a killed driver's leftover
        json.dump({**_dead_identity(), "label": "cell-a"}, fh)
    with R.exclusive(p, "cell-a"):
        assert json.loads(open(p, encoding="utf-8").read())["pid"] == os.getpid()


def test_lock_of_another_host_is_refused(tmp_path):
    p = str(tmp_path / ".lock")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"host": "remote-box", "pid": 1, "pid_started": 1.0, "label": "x"}, fh)
    with pytest.raises(R.ResumeRefused, match="remote-box"):
        with R.exclusive(p, "x"):
            pass
