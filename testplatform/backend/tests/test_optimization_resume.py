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
from datetime import datetime

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
    """A process that existed and is gone: a finished child's pid + its recorded identity."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    ident = R.process_identity(p.pid)
    p.wait()
    return ident


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

LINUX_ME = {"boot_id": "boot-1", "start_ticks": 4242, "cmdline_sha": "me"}


def _as_linux(monkeypatch, now):
    """Read the process identity as Linux /proc would give it."""
    monkeypatch.setattr(R, "proc_identity", lambda pid: dict(now))


def test_this_process_is_alive():
    me = R.process_identity()
    assert R.judge(me["host"], me["pid"], me["pid_started"], me["proc_identity"])[0] == R.ALIVE


def test_a_finished_process_is_dead():
    d = _dead_identity()
    assert R.judge(d["host"], d["pid"], d["pid_started"], d["proc_identity"])[0] == R.DEAD


def test_a_start_time_mismatch_alone_is_unknown_never_dead():
    """A clock step on a VM moves psutil's create_time on Linux: the LIVE runner must not be
    judged dead. Same pid, same command line, start time off by an hour: UNKNOWN (refused)."""
    me = R.process_identity()
    state, why = R.judge(me["host"], me["pid"], me["pid_started"] - 3600.0,
                         {"cmdline_sha": me["proc_identity"].get("cmdline_sha")})
    assert state == R.UNKNOWN and "clock step" in why


def test_linux_start_tick_decides_whatever_the_wall_clock_says(monkeypatch):
    me = R.process_identity()
    _as_linux(monkeypatch, LINUX_ME)
    # same kernel start tick, create_time shifted by a clock step: ALIVE
    assert R.judge(me["host"], me["pid"], me["pid_started"] - 3600.0, LINUX_ME)[0] == R.ALIVE
    # another start tick in the same boot: the pid was reused -> DEAD
    assert R.judge(me["host"], me["pid"], me["pid_started"],
                   {**LINUX_ME, "start_ticks": 1})[0] == R.DEAD
    # recorded in another boot: that process cannot be alive -> DEAD
    assert R.judge(me["host"], me["pid"], me["pid_started"],
                   {**LINUX_ME, "boot_id": "boot-0"})[0] == R.DEAD


def test_another_command_line_proves_reuse():
    me = R.process_identity()
    state, why = R.judge(me["host"], me["pid"], me["pid_started"] - 3600.0,
                         {"cmdline_sha": "some-other-command"})
    assert state == R.DEAD and "reused" in why


def test_nothing_recorded_is_unknown_and_another_host_is_foreign():
    me = R.process_identity()
    assert R.judge(None, me["pid"], None)[0] == R.UNKNOWN
    assert R.judge("some-other-box", me["pid"], me["pid_started"])[0] == R.FOREIGN


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


def test_running_row_whose_runner_cannot_be_proven_dead_is_refused(S):
    n = _name("unknown")
    oid = _opt(S, n)
    me = R.process_identity()
    _register(S, oid, {**me, "pid_started": me["pid_started"] - 3600.0,
                       "proc_identity": {"cmdline_sha": me["proc_identity"].get("cmdline_sha")}})
    db = S()
    try:
        with pytest.raises(R.ResumeRefused, match="UNKNOWN.*only a runner proven dead"):
            R.assess(db, n)
    finally:
        db.close()


def test_completed_row_whose_topn_persist_is_still_running_is_refused(S):
    """The launcher of a completed job may still be persisting its TOP-N (only the driver was
    killed): never touched while that process lives."""
    n = _name("topn-live")
    oid = _opt(S, n, status="completed")
    _register(S, oid, R.process_identity())
    db = S()
    try:
        with pytest.raises(R.ResumeRefused, match="LIVE process"):
            R.assess(db, n)
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


def test_seeded_from_must_match_the_new_chain_seed_source(S):
    """No checkpoint yet means the chain seed is applied again: it must be the same source row."""
    oid = _stale(S, cfg={**_cfg(chainSeedFromOptimizationId=5),
                         "seededFrom": {"kind": "chain", "optimization_id": 5, "name": "c-wf1"}})
    assert _diffs(S, oid, new_cfg=_cfg(chainSeedFromOptimizationId=5)) == []
    d = _diffs(S, oid, new_cfg=_cfg(chainSeedFromOptimizationId=6))
    assert any(x.startswith("seededFrom") for x in d)


def test_job_identity_hash_follows_window_and_data_but_not_exempt_keys():
    h = lambda cfg: R.job_identity_hash(cfg, "sharpe", "genetic", {"x": {}}, [{"id": "r"}], [])
    base = h(_cfg())
    moved = _cfg()
    moved["backtest"]["end_date"] = "2024-12-31"
    digest = _cfg()
    digest["backtest"]["market_condition_manifests"] = {"ohlcv-v1": "sha-B"}
    assert h(moved) != base and h(digest) != base
    assert h({**_cfg(parallelIndividuals=9), "seededFrom": {"x": 1}, "resumedFrom": {"y": 2}}) == base


# ------------------------------------------------------------------ claim / retire


def test_claim_frees_the_name_and_keeps_the_row(S):
    from app.models.strategy_optimization import StrategyOptimization as SO
    n = _name("ret")
    oid = _opt(S, n)
    db = S()
    try:
        new_name = R.claim_and_retire(db, oid, n, "resumed as #77")
        db.commit()
        row = db.get(SO, oid)
        assert row.name == new_name == f"{n}~interrupted-{oid}"
        assert row.status == "failed" and "INTERRUPTED" in row.error_message
        assert "resumed as #77" in row.error_message
        assert R.assess(db, n).kind == "none"
        assert row.all_results == [{"key": "k1", "fitness": 1.0}]       # evidence kept
    finally:
        db.close()


def test_two_competing_claims_only_one_wins(S):
    """Both starters passed assess(); the second claim matches no row and is refused, and its
    whole transaction (the new row it inserted) is rolled back."""
    from app.models.strategy_optimization import StrategyOptimization as SO
    n = _name("race")
    oid = _opt(S, n)
    _register(S, oid, _dead_identity())
    a, b = S(), S()
    try:
        assert R.assess(a, n).kind == "resume" and R.assess(b, n).kind == "resume"
        sid = a.get(SO, oid).strategy_id
        for db in (a, b):
            db.commit()
        a.add(SO(strategy_id=sid, name=n, fitness_metric="sharpe", optimization_type="genetic",
                 optimization_config=_cfg(), status="pending"))
        a.flush()
        R.claim_and_retire(a, oid, n, "A")
        a.commit()
        b.add(SO(strategy_id=sid, name=n, fitness_metric="sharpe", optimization_type="genetic",
                 optimization_config=_cfg(), status="pending"))
        b.flush()
        with pytest.raises(R.ResumeRefused, match="claimed by another starter"):
            R.claim_and_retire(b, oid, n, "B")
        b.rollback()
        rows = a.query(SO.status, SO.error_message).filter(SO.name == n).all()
        assert [r[0] for r in rows] == ["pending"]                       # exactly one new row
        assert "A" in a.get(SO, oid).error_message
    finally:
        a.close()
        b.close()


def test_a_half_written_backtest_is_retired_only_when_its_writer_is_dead(S):
    from app.models.backtest import Backtest
    db = S()
    try:
        bt = Backtest(name=_name("WF1-OOS-R1"), engine_type="daily_expert", status="running",
                      initial_capital=1.0, start_date=datetime(2026, 1, 1), end_date=datetime(2026, 6, 30))
        db.add(bt); db.flush()
        R.register_backtest_writer(db, bt.id, identity=R.process_identity())
        db.commit()
        with pytest.raises(R.ResumeRefused, match="ALIVE"):
            R.retire_dead_backtest(db, bt.id, bt.name, "test")
        legacy = Backtest(name=_name("TOP2"), engine_type="daily_expert", status="running",
                          initial_capital=1.0, start_date=datetime(2026, 1, 1),
                          end_date=datetime(2026, 6, 30))
        db.add(legacy); db.commit()
        with pytest.raises(R.ResumeRefused, match="UNKNOWN.*no writer was recorded"):
            R.retire_dead_backtest(db, legacy.id, legacy.name, "test")
        R.register_backtest_writer(db, bt.id, identity=_dead_identity())
        db.commit()
        assert R.retire_dead_backtest(db, bt.id, bt.name, "test").endswith(f"~interrupted-{bt.id}")
    finally:
        db.close()


# ------------------------------------------------------------------ the driver's lock


_HOLDER = r"""
import sys, time
sys.path.insert(0, sys.argv[2])
from app.services import optimization_resume as R
h = R.acquire_lock(sys.argv[1], "cell-a")
print("locked", flush=True)
time.sleep(120)
"""


def test_lock_is_an_os_lock_held_by_a_live_process_and_freed_by_its_death(tmp_path):
    p = str(tmp_path / "cell" / ".lock")
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, p, backend], stdout=subprocess.PIPE,
                              text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(R.ResumeRefused, match="held by a LIVE process"):
            R.acquire_lock(p, "cell-a")
    finally:
        # our own child (and the interpreter a venv launcher starts under it), by pid: what
        # `grid stop` does to a driver
        import psutil
        for proc in psutil.Process(holder.pid).children(recursive=True):
            proc.kill()
        holder.kill()
        holder.wait()
    with R.exclusive(p, "cell-a"):  # the OS released it with the dead holder
        held = json.loads(open(p, encoding="utf-8").read())
        assert held["pid"] == os.getpid() and held["label"] == "cell-a"
        with pytest.raises(R.ResumeRefused, match="LIVE process"):
            R.acquire_lock(p, "cell-a")          # a second driver, same host, while we live
    with R.exclusive(p, "cell-a"):               # released on exit: re-acquirable
        pass


def test_lock_file_content_plays_no_part_in_the_decision(tmp_path):
    """A leftover file naming nobody (or anybody) is not a lock: only the OS lock is."""
    p = tmp_path / ".lock"
    p.write_text(json.dumps({"host": "remote-box", "pid": 1, "label": "x"}))
    with R.exclusive(str(p), "x"):
        pass
