"""Owner decision 2026-10-07: "an analysis failure should fail the JOB".

A run where analyses fail en masse is a data or code problem, not a property of one genome. The first
trial that ends with ``AnalysisFailureRefusal`` (or any other job-fatal type: ``StaleAnchorPrice`` ...)
aborts the whole optimization: no further trial is dispatched, the ``strategy_optimizations`` row is
marked failed with a reason naming the expert, the counts and the trial that tripped it.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services import strategy_optimization_handler as H
from app.services.backtest.daily_engine import AnalysisFailureRefusal


def _refusal():
    return AnalysisFailureRefusal(
        "Backtest refused: expert 7: 90 of 100 analysis passes failed (90% > 5%); the result would "
        "measure a broken expert, not the strategy. First error: boom", expert_id=7, passes=100,
        failed=90, first_error="boom")


# ----------------------------------------------------------------------- classification
@pytest.mark.parametrize("name", ["AnalysisFailureRefusal", "StaleAnchorPrice", "StaleMarkToMarket",
                                  "ComboSettlementRefused", "BacktestCacheMiss", "SplitBasisRefused",
                                  "MarketCalendarUnavailable"])
def test_these_types_end_the_job(name):
    assert H.job_fatal(name) and name in H.JOB_FATAL_ERROR_TYPES


@pytest.mark.parametrize("exc", [ValueError("a bad genome"), TypeError("x"), KeyError("k"),
                                 InterruptedError("paused/cancelled")])
def test_an_ordinary_error_does_not(exc):
    assert not H.job_fatal(exc)


def test_the_real_exception_objects_are_classified_by_type():
    from ba2_common.core.knowability import StaleAnchorPrice
    assert H.job_fatal(_refusal()) and H.job_fatal(StaleAnchorPrice("plain float anchor"))


# ----------------------------------------------------------------------- worker -> master round trip
def _worker_result(monkeypatch, exc):
    import app.services.backtest.daily_backtest_handler as dbh

    def boom(config, progress_cb=None):
        raise exc

    monkeypatch.setattr(dbh, "run_daily_backtest", boom)
    out = H._trial_worker({"symbols": ["NFLX"]}, "calmar")
    return json.loads(json.dumps(out, default=str))       # what a REMOTE worker's result is on the wire


def test_the_exception_type_travels_back_to_the_master(monkeypatch):
    out = _worker_result(monkeypatch, _refusal())
    assert out["ok"] is False and out["fatal"] is True
    assert out["error_type"] == "AnalysisFailureRefusal"
    assert "90 of 100" in out["error"] and "boom" in out["error"]            # the reason, not a repr()


def test_the_master_classifies_by_the_type_even_when_a_worker_sent_no_fatal_flag(monkeypatch):
    out = _worker_result(monkeypatch, _refusal())
    out["fatal"] = False                                   # an older / different worker build
    fatal = {"msg": None}
    with pytest.raises(H._FatalTrialError) as ei:
        H._abort_on_fatal_trial(out, fatal, "trial-key-1", {"x": 1.5})
    msg = str(ei.value)
    assert fatal["msg"] == msg
    for needle in ("90 of 100", "job-fatal AnalysisFailureRefusal", "trial-key-1", '"x": 1.5'):
        assert needle in msg


def test_a_stale_anchor_price_ends_the_job_too(monkeypatch):
    from ba2_common.core.knowability import StaleAnchorPrice
    out = _worker_result(monkeypatch, StaleAnchorPrice("anchor 1.0 is not the decision price"))
    assert out["fatal"] is True and out["error_type"] == "StaleAnchorPrice"
    with pytest.raises(H._FatalTrialError):
        H._abort_on_fatal_trial(out, {"msg": None}, "k", {})


def test_an_ordinary_failed_trial_is_still_just_a_sentinel(monkeypatch):
    out = _worker_result(monkeypatch, ValueError("a bad genome"))
    assert out["fatal"] is False and out["error_type"] == "ValueError"
    H._abort_on_fatal_trial(out, {"msg": None}, "k", {})       # returns: no abort


# ----------------------------------------------------------------------- the real handler (serial GA)
def _env(monkeypatch):
    import test_strategy_optimization_handler as T
    from app.models.database import Base, engine
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    return T


def test_a_job_whose_first_trial_trips_the_refusal_fails_and_dispatches_nothing_more(monkeypatch):
    T = _env(monkeypatch)
    calls = []

    def stub(backtest_cfg, hoisted, decoded):
        calls.append(1)
        raise _refusal()

    monkeypatch.setattr(H, "_run_trial_backtest", stub)
    sid = T._seed_strategy()
    opt_id = T._seed_opt(sid, config=T._ga_config(populationSize=4, generations=3))
    out = H.handle_strategy_optimization("t-af-fatal", {"optimization_id": opt_id})
    assert out["status"] == "failed", out
    assert "job-fatal AnalysisFailureRefusal" in out["error"] and "90 of 100" in out["error"]
    assert len(calls) == 1                                  # not one more trial after the first
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization
    db = SessionLocal()
    try:
        row = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
        assert row.status == "failed" and "AnalysisFailureRefusal" in row.error_message
    finally:
        db.close()


def test_a_job_with_failures_under_the_threshold_completes_and_reports_the_aggregate(monkeypatch, caplog):
    import logging
    T = _env(monkeypatch)
    real_stub = T._deterministic_stub

    def stub(backtest_cfg, hoisted, decoded):
        res = dict(real_stub(backtest_cfg, hoisted, decoded))
        res["analysis_failures"] = {"passes": 200, "failed": 4, "first_error": "one flaky symbol"}   # 2%
        return res

    monkeypatch.setattr(H, "_run_trial_backtest", stub)
    caplog.set_level(logging.INFO)
    sid = T._seed_strategy()
    opt_id = T._seed_opt(sid, config=T._ga_config(populationSize=4, generations=2))
    out = H.handle_strategy_optimization("t-af-ok", {"optimization_id": opt_id})
    assert out["status"] == "completed", out
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization
    db = SessionLocal()
    try:
        row = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
        rows = [r for r in row.all_results if r.get("af")]
        assert rows and all(r["af"] == {"passes": 200, "failed": 4} for r in rows)
    finally:
        db.close()
    lines = [r.getMessage() for r in caplog.records if "analysis passes over" in r.getMessage()]
    assert len(lines) == 1 and "2.00%" in lines[0]


def test_the_summary_aggregates_across_trials_and_names_the_worst():
    agg = H.summarise_analysis_failures("job", [
        {"key": "a", "af": {"passes": 100, "failed": 1}},
        {"key": "b", "af": {"passes": 100, "failed": 4}},
        {"key": "c"},
    ])
    assert agg == {"trials": 2, "passes": 200, "failed": 5, "worst_share": 0.04, "worst_trial": "b"}
    assert H.summarise_analysis_failures("job", [{"key": "x"}]) is None


# ----------------------------------------------------------------------- the grid drivers
def _convex_driver():
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    path = os.path.join(root, "tools", "run_convex_matrix.py")
    spec = importlib.util.spec_from_file_location("run_convex_matrix_job_fatal", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules["run_convex_matrix_job_fatal"] = m
    spec.loader.exec_module(m)
    return m


def test_a_driver_run_with_one_failing_job_continues_reports_it_and_exits_non_zero(monkeypatch, capsys):
    d = _convex_driver()
    monkeypatch.setattr(d, "_completed_names", lambda: set())
    launched = []

    class _Done:
        def __init__(self, rc): self.returncode = rc

    def fake_run(cmd, env=None, **kw):
        name = cmd[cmd.index("--name") + 1]
        launched.append(name)
        return _Done(1 if len(launched) == 1 else 0)         # the FIRST job fails (analysis refusal)

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    rc = d.main(["--skip-preflight"])
    out = capsys.readouterr().out
    assert len(launched) > 1                                   # the driver went on after the failure
    assert rc == 1
    assert "FAILED" in out and launched[0] in out and "1 job(s) FAILED" in out


def test_a_clean_driver_run_exits_zero(monkeypatch, capsys):
    d = _convex_driver()
    monkeypatch.setattr(d, "_completed_names", lambda: set())

    class _Done:
        returncode = 0

    monkeypatch.setattr(d.subprocess, "run", lambda cmd, env=None, **kw: _Done())
    assert d.main(["--skip-preflight"]) == 0


def test_the_options_matrix_recognises_an_analysis_refusal_reason_and_nothing_else():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "tools"))
    from matrix_flags import is_analysis_failure_reason
    ok = ("Backtest refused: expert 7: 90 of 100 analysis passes failed  "
          "[job-fatal AnalysisFailureRefusal; tripped by trial key k, params {}]")
    assert is_analysis_failure_reason(ok)
    assert not is_analysis_failure_reason("BacktestCacheMiss: x  [job-fatal BacktestCacheMiss; ...]")
    assert not is_analysis_failure_reason("")
