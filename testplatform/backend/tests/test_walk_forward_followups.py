"""Walk-forward engine review follow-ups: OOS rows must not contaminate the train job's own rows,
the UI re-run honours a row's own window, state-importing optimize flags are refused, the real
runner's DB access / refusals / logging, and the report contents. See test_walk_forward.py for
the core engine tests (helpers are reused from there)."""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

import pytest

from app.services import walk_forward as WF

from .test_walk_forward import D, FakeRunner, _oos, _pick, _result, _tool

# ------------------------------------------------------------------ flags, report


@pytest.mark.parametrize("args", [["--warm-start-from", "12"], ["--warm-start-from=12"],
                                  ["--rerun"], ["--submit"]])
def test_state_importing_and_resume_breaking_flags_are_refused(args):
    with pytest.raises(WF.WalkForwardError, match="must not contain"):
        WF.check_user_optimize_args(["--expert", "X", *args])


def test_selection_defaults_come_from_persist_distinct_topn_and_reach_the_report(tmp_path):
    tool = _tool()
    d = tool.selection_defaults()
    import persist_distinct_topn as P
    ns = P._parse(["--opt-id", "1"])
    assert d == {"min_trade_rows": ns.min_trade_rows, "min_return_rel_pct": ns.min_return_rel_pct,
                 "min_dd_pts": ns.min_dd_pts, "min_trades_rel_pct": ns.min_trades_rel_pct}
    res = _result([_oos(5.0, -25.0), _oos(3.0, -30.0)], planned=2)
    res.selection = dict(d)
    assert "min_dd_pts=" in WF.render_markdown(res)
    assert json.loads(Path(WF.write_report(res, str(tmp_path))[0]).read_text())["selection"] == d


def test_report_labels_worst_dd_as_single_fold_and_oos_return_as_total():
    md = WF.render_markdown(_result([_oos(5.0, -25.0), _oos(3.0, -30.0)], planned=2))
    assert "worst single-fold DD" in md and "NOT a chained drawdown" in md
    assert "NOT annualised" in md and "(1.00y)" in md


def test_cli_selection_override_is_used_and_shown(capsys):
    tool = _tool()
    tool.main(["--prefix", "s", "--train-start", "2020-01-01", "--test-years", "2024", "--top-n",
               "1", "--pass-min-oos-return", "0", "--pass-max-dd-mult", "1", "--pass-min-folds",
               "1", "--min-dd-pts", "7", "--dry-run", "--", "--expert", "X"], runner=FakeRunner())
    assert "'min_dd_pts': 7.0" in capsys.readouterr().out


def test_oos_row_markers_and_window_helper():
    from app.services.distinct_topn import is_oos_row, row_in_window
    assert is_oos_row("WF2-OOS-R1-job", None) and is_oos_row("x", ["TopN", "OOS"])
    assert is_oos_row("x", '["OOS"]') and not is_oos_row("TOP1-job", ["TopNDistinct"])
    assert not is_oos_row("WFX-job", None)
    assert row_in_window(datetime(2020, 1, 1), datetime(2025, 12, 31), "2020-01-01", "2025-12-31")
    assert not row_in_window(datetime(2026, 1, 1), datetime(2026, 6, 30), "2020-01-01",
                             "2025-12-31")


# ------------------------------------------------------------------ DB-backed


@pytest.fixture
def host_db():
    import app.models  # noqa: F401
    from app.models.database import Base, SessionLocal, engine
    Base.metadata.create_all(bind=engine)
    return SessionLocal


def _seed_opt(S, name, status="completed", start="2020-01-01", end="2025-12-31"):
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    db = S()
    try:
        strat = Strategy(name=f"s-{name}", entry_rules=[], exit_rules=[])
        db.add(strat); db.commit(); db.refresh(strat)
        opt = StrategyOptimization(
            strategy_id=strat.id, name=name, fitness_metric="sharpe", optimization_type="genetic",
            optimization_config={"backtest": {
                "backtest_id": 1, "start_date": start, "end_date": end,
                "enabled_instruments": ["AAPL"],
                "experts": [{"class": "FMPRating", "settings": {}}],
                "initial_capital": 10000.0, "account_settings": {"starting_cash": 10000.0},
                "warmup_days": 30, "seed": 42}},
            all_results=[], best_params={}, best_fitness=1.0, status=status)
        db.add(opt); db.commit(); db.refresh(opt)
        return opt.id
    finally:
        db.close()


def _seed_bt(S, opt_id, name, start, end, status="completed", labels=None, ga_fitness=1.0,
             ret=5.0, dd=-4.0, trades=10):
    from app.models.backtest import Backtest
    db = S()
    try:
        bt = Backtest(name=name, engine_type="daily_expert", expert_name="FMPRating",
                      optimization_id=opt_id, labels=labels, strategy_params={"g": 1},
                      start_date=start, end_date=end, initial_capital=10000.0, status=status,
                      total_return=ret, max_drawdown=dd, total_trades=trades,
                      ga_fitness=ga_fitness, is_saved=True)
        db.add(bt); db.commit(); db.refresh(bt)
        return bt.id
    finally:
        db.close()


def test_existing_rows_ignore_oos_rows_so_skip_already_persisted_still_persists(host_db):
    import persist_distinct_topn as P
    opt_id = _seed_opt(host_db, "wf-contam")
    oos = _seed_bt(host_db, opt_id, "WF3-OOS-R1-wf-contam", datetime(2026, 1, 1),
                   datetime(2026, 6, 30), labels=["WalkForward", "OOS"])
    other = _seed_bt(host_db, opt_id, "mystery", datetime(2026, 1, 1), datetime(2026, 6, 30))
    ins = _seed_bt(host_db, opt_id, "DTOP1-wf-contam", datetime(2020, 1, 1),
                   datetime(2025, 12, 31))

    def ids():
        db = host_db()
        try:
            return [r[0] for r in P._existing_rows(db, opt_id, ("2020-01-01", "2025-12-31"))]
        finally:
            db.close()
    assert ids() == [ins]                       # OOS (label + window) and the other-window row out
    assert oos not in ids() and other not in ids()
    # an OOS row that (oddly) carries the optimization's own window is still excluded, by label
    _seed_bt(host_db, opt_id, "WF1-OOS-R1-wf-contam", datetime(2020, 1, 1),
             datetime(2025, 12, 31), labels=["OOS"])
    assert ids() == [ins]


def test_rerun_of_an_oos_row_uses_the_rows_own_window(host_db):
    from app.models.backtest import Backtest
    from app.services.backtest import rerun_handler as RH
    opt_id = _seed_opt(host_db, "wf-rerun")
    oos = _seed_bt(host_db, opt_id, "WF1-OOS-R1-wf-rerun", datetime(2026, 1, 1),
                   datetime(2026, 6, 30), labels=["OOS"])
    ins = _seed_bt(host_db, opt_id, "TOP1-wf-rerun", datetime(2020, 1, 1), datetime(2025, 12, 31))
    db = host_db()
    try:
        cfg = RH._build_optimization_rerun_config(db, db.get(Backtest, oos))
        assert (str(cfg["start_date"])[:10], str(cfg["end_date"])[:10]) == ("2026-01-01",
                                                                           "2026-06-30")
        cfg = RH._build_optimization_rerun_config(db, db.get(Backtest, ins))
        assert (str(cfg["start_date"])[:10], str(cfg["end_date"])[:10]) == ("2020-01-01",
                                                                           "2025-12-31")
    finally:
        db.close()


# ------------------------------------------------------------------ RealRunner


class _FakeLauncher:
    """Stands in for ba2test_launcher: records the call, persists one completed row per pick."""

    def __init__(self, S):
        self.S = S
        self.calls = []

    def _persist_top_backtests(self, opt_id, expert, **kw):
        self.calls.append((kw["name_prefix"], kw["window"], logging.root.manager.disable))
        for (params, key, fit), rank in zip(kw["candidates"], kw["ranks"]):
            bid = _seed_bt(self.S, opt_id, f"{kw['name_prefix']}{rank}-wf-r",
                           datetime.fromisoformat(kw["window"][0]),
                           datetime.fromisoformat(kw["window"][1]), ga_fitness=fit,
                           labels=kw["extra_labels"])
            kw["persisted_ids"].append((rank, bid))
        return len(kw["candidates"])


def _real_runner(monkeypatch, host_db):
    tool = _tool()
    import persist_distinct_topn as P
    r = tool.RealRunner(argparse.Namespace(min_free_gb=20.0, force_memory=False))
    fake = _FakeLauncher(host_db)
    r._persist_tool, r._launcher = P, fake
    monkeypatch.setattr(r, "_tool", lambda: P)
    monkeypatch.setattr(P, "_check_memory", lambda args: None)
    return r, fake, P


def _fold(k=1):
    return WF.Fold(k, D("2020-01-01"), D("2025-12-31"), D("2026-01-01"), D("2026-06-30"))


def test_train_completed_reads_the_test_db_through_session_local(monkeypatch, host_db):
    r, _, _ = _real_runner(monkeypatch, host_db)
    assert r.train_completed("wf-none") is False
    _seed_opt(host_db, "wf-done")
    _seed_opt(host_db, "wf-failed", status="failed")
    assert r.train_completed("wf-done") is True and r.train_completed("wf-failed") is False


def test_duplicate_optimization_name_is_refused_up_front(monkeypatch, host_db):
    r, _, _ = _real_runner(monkeypatch, host_db)
    a = _seed_opt(host_db, "wf-dup", status="failed")
    b = _seed_opt(host_db, "wf-dup")
    with pytest.raises(WF.WalkForwardError) as e:
        r.train_completed("wf-dup")
    assert f"id={a} failed" in str(e.value) and f"id={b} completed" in str(e.value)
    assert "Delete" in str(e.value)


def test_a_non_completed_leftover_blocks_a_second_train_run(monkeypatch, host_db):
    r, _, _ = _real_runner(monkeypatch, host_db)
    _seed_opt(host_db, "wf-left", status="running")
    with pytest.raises(WF.WalkForwardError, match="duplicate name"):
        r.run_train(["x"], "wf-left")


def test_low_memory_refusal_becomes_a_walk_forward_error(monkeypatch, host_db):
    r, _, P = _real_runner(monkeypatch, host_db)
    r._opt["wf-r"] = (_seed_opt(host_db, "wf-r"), "FMPRating")

    def refuse(args):
        raise P.Refused("MemAvailable 3.0 GB < --min-free-gb 20")
    monkeypatch.setattr(P, "_check_memory", refuse)
    with pytest.raises(WF.WalkForwardError, match="MemAvailable"):
        r.run_oos("wf-r", _fold(), [_pick(1)], "WF1-OOS-R", ["OOS"], 1)


def test_running_oos_row_from_a_crash_is_refused_not_duplicated(monkeypatch, host_db):
    r, fake, _ = _real_runner(monkeypatch, host_db)
    opt_id = _seed_opt(host_db, "wf-r")
    r._opt["wf-r"] = (opt_id, "FMPRating")
    bid = _seed_bt(host_db, opt_id, "WF1-OOS-R1-wf-r", datetime(2026, 1, 1),
                   datetime(2026, 6, 30), status="running")
    with pytest.raises(WF.WalkForwardError, match=f"id {bid}.*running"):
        r.run_oos("wf-r", _fold(), [_pick(1)], "WF1-OOS-R", ["OOS"], 1)
    assert fake.calls == []


def test_run_oos_passes_the_window_reuses_completed_rows_and_restores_logging(monkeypatch,
                                                                              host_db):
    r, fake, _ = _real_runner(monkeypatch, host_db)
    opt_id = _seed_opt(host_db, "wf-r")
    r._opt["wf-r"] = (opt_id, "FMPRating")
    _seed_bt(host_db, opt_id, "WF1-OOS-R1-wf-r", datetime(2026, 1, 1), datetime(2026, 6, 30),
             ga_fitness=1.0, ret=7.0, dd=-3.0, trades=9)
    logging.disable(logging.NOTSET)
    out = r.run_oos("wf-r", _fold(), [_pick(1, fit=1.0), _pick(2, fit=2.0)], "WF1-OOS-R",
                    ["WalkForward", "wf-fold-1", "OOS"], 1)
    assert logging.root.manager.disable == logging.NOTSET           # restored
    assert out[1].reused and out[1].total_return == 7.0 and out[1].trades == 9
    assert not out[2].reused
    assert fake.calls == [("WF1-OOS-R", ("2026-01-01", "2026-06-30"), logging.WARNING)]
