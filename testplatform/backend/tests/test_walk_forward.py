"""Walk-forward engine: fold construction, validation, resume, aggregation, verdict, dry-run, and
the option-holdout rail interaction. Pure logic + a fake runner; nothing is optimized.

``app.services.walk_forward`` is the pure module, ``tools/run_walk_forward.py`` the CLI. The
rail tests drive the REAL ``ba2test_launcher._assert_option_window_excludes_holdout`` and the real
``_persist_top_backtests(window=...)`` (its worker monkeypatched) -- the rail is neither changed
nor bypassed by the engine.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
if _root not in sys.path:
    sys.path.insert(0, _root)

from app.services import walk_forward as WF  # noqa: E402

_REPO = Path(__file__).resolve().parents[3]


def _tool():
    spec = importlib.util.spec_from_file_location("run_walk_forward_tool",
                                                  str(_REPO / "tools" / "run_walk_forward.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _launcher_mod():
    spec = importlib.util.spec_from_file_location(
        "ba2test_launcher", str(_REPO / "testplatform" / "ba2test_launcher.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


D = date.fromisoformat
TH = WF.Thresholds(min_oos_return=0.0, max_dd_mult=1.5, min_folds=2)


# ------------------------------------------------------------------ fold construction


def test_anchored_expanding_year_folds():
    folds = WF.build_year_folds(D("2020-01-01"), [2024, 2025, 2026])
    assert [(f.index, f.train_start, f.train_end, f.test_start, f.test_end) for f in folds] == [
        (1, D("2020-01-01"), D("2023-12-31"), D("2024-01-01"), D("2024-12-31")),
        (2, D("2020-01-01"), D("2024-12-31"), D("2025-01-01"), D("2025-12-31")),
        (3, D("2020-01-01"), D("2025-12-31"), D("2026-01-01"), D("2026-12-31")),
    ]


def test_partial_last_year_via_test_end():
    folds = WF.build_year_folds(D("2020-01-01"), [2025, 2026], test_end=D("2026-06-30"))
    assert folds[-1].test_end == D("2026-06-30") and folds[0].test_end == D("2025-12-31")


def test_test_end_outside_last_year_is_refused():
    with pytest.raises(WF.WalkForwardError, match="last test year"):
        WF.build_year_folds(D("2020-01-01"), [2024, 2025], test_end=D("2026-06-30"))


def test_embargo_moves_the_train_end_back_and_keeps_the_test_year_whole():
    f = WF.build_year_folds(D("2020-01-01"), [2024], embargo_days=30)[0]
    assert f.test_start == D("2024-01-01")
    assert f.train_end == D("2023-12-01")      # 30 full days between 12-01 and 01-01 exclusive
    assert (f.test_start - f.train_end).days - 1 == 30
    WF.validate_folds([f], embargo_days=30)


def test_explicit_folds_parse_and_validate():
    folds = [WF.parse_explicit_fold("2020-01-01:2022-12-31:2023-01-01:2023-12-31", 1),
             WF.parse_explicit_fold("2020-01-01:2023-12-31:2024-02-01:2024-12-31", 2)]
    WF.validate_folds(folds)
    WF.validate_folds(folds, embargo_days=0)
    with pytest.raises(WF.WalkForwardError, match="embargo"):
        WF.validate_folds(folds[:1], embargo_days=5)


@pytest.mark.parametrize("text", ["2020-01-01:2022-12-31:2023-01-01", "2020-01-01:x:2023-01-01:2023-12-31"])
def test_malformed_explicit_fold_is_refused(text):
    with pytest.raises(WF.WalkForwardError):
        WF.parse_explicit_fold(text, 1)


def test_refuses_test_overlapping_train():
    f = WF.parse_explicit_fold("2020-01-01:2024-06-30:2024-06-30:2024-12-31", 1)
    with pytest.raises(WF.WalkForwardError, match="overlap"):
        WF.validate_folds([f])


def test_refuses_test_before_train():
    f = WF.parse_explicit_fold("2020-01-01:2024-06-30:2023-01-01:2023-12-31", 1)
    with pytest.raises(WF.WalkForwardError, match="not after train end"):
        WF.validate_folds([f])


def test_refuses_folds_out_of_order_and_overlapping_tests():
    a = WF.parse_explicit_fold("2020-01-01:2022-12-31:2023-01-01:2023-12-31", 1)
    b = WF.parse_explicit_fold("2020-01-01:2023-12-31:2024-01-01:2024-12-31", 2)
    with pytest.raises(WF.WalkForwardError, match="ordered"):
        WF.validate_folds([WF.Fold(1, b.train_start, b.train_end, b.test_start, b.test_end),
                           WF.Fold(2, a.train_start, a.train_end, a.test_start, a.test_end)])
    c = WF.parse_explicit_fold("2020-01-01:2023-12-31:2024-01-01:2024-12-31", 2)
    d = WF.parse_explicit_fold("2020-01-01:2023-12-31:2024-12-31:2025-06-30", 2)
    with pytest.raises(WF.WalkForwardError, match="ordered"):
        WF.validate_folds([c, d])


def test_unordered_test_years_refused():
    with pytest.raises(WF.WalkForwardError, match="ascending"):
        WF.build_year_folds(D("2020-01-01"), [2025, 2024])


@pytest.mark.parametrize("args", [["--start", "2020-01-01"], ["--end=2024-01-01"],
                                  ["--expert", "X", "--name", "n"], ["--name=n"]])
def test_user_args_with_window_or_name_are_refused(args):
    with pytest.raises(WF.WalkForwardError, match="must not contain"):
        WF.check_user_optimize_args(args)


def test_user_args_without_them_are_accepted():
    WF.check_user_optimize_args(["--expert", "FMPRating", "--startup-x", "1"])


def test_thresholds_must_fit_the_fold_count():
    with pytest.raises(WF.WalkForwardError, match="pass-min-folds"):
        WF.Thresholds(0.0, 1.5, 4).validate(3)
    with pytest.raises(WF.WalkForwardError, match="pass-max-dd-mult"):
        WF.Thresholds(0.0, 0.0, 1).validate(3)


def test_missing_threshold_flag_is_refused_by_the_cli():
    tool = _tool()
    base = ["--prefix", "p", "--train-start", "2020-01-01", "--test-years", "2024", "--top-n", "2",
            "--pass-min-oos-return", "0", "--pass-max-dd-mult", "1.5", "--pass-min-folds", "1"]
    tool._parse(base)                                # complete: fine
    for flag in ("--pass-min-oos-return", "--pass-max-dd-mult", "--pass-min-folds", "--top-n"):
        i = base.index(flag)
        with pytest.raises(SystemExit) as exc:
            tool._parse(base[:i] + base[i + 2:])
        assert exc.value.code == 2


def test_cli_splits_optimize_args_after_double_dash():
    tool = _tool()
    a, opt = tool._parse(["--prefix", "p", "--top-n", "1", "--pass-min-oos-return", "0",
                          "--pass-max-dd-mult", "1", "--pass-min-folds", "1", "--",
                          "--expert", "X", "--start", "2020-01-01"])
    assert opt == ["--expert", "X", "--start", "2020-01-01"]
    with pytest.raises(WF.WalkForwardError, match="must not contain"):
        WF.check_user_optimize_args(opt)


# ------------------------------------------------------------------ fake runner


class FakeRunner:
    def __init__(self, completed=(), is_picks=None, oos=None):
        self.completed = set(completed)
        self.trained = []
        self.tested = []
        self.is_picks = is_picks or {}
        self.oos = oos or {}

    def train_completed(self, name):
        return name in self.completed

    def run_train(self, cmd, name):
        self.trained.append((name, cmd))
        self.completed.add(name)

    def select(self, train_name, top_n):
        return self.is_picks[train_name][:top_n]

    def run_oos(self, train_name, fold, picks, name_prefix, labels, parallel):
        self.tested.append((train_name, fold.index, name_prefix, list(labels), parallel,
                            [p.rank for p in picks]))
        return {p.rank: self.oos[(fold.index, p.rank)] for p in picks}


def _pick(rank, ret=100.0, car=25.0, dd=-20.0, trades=50, fit=1.0):
    return WF.IsPick(rank=rank, fitness=fit, total_return=ret, car=car, max_drawdown=dd,
                     trades=trades)


def _oos(ret, dd, trades=10):
    return WF.OosMetrics(backtest_id=1, total_return=ret, max_drawdown=dd, trades=trades)


def _plan(folds=None, **kw):
    folds = folds or WF.build_year_folds(D("2020-01-01"), [2024, 2025, 2026], D("2026-06-30"))
    args = dict(test_parallel=1, skip_train=False, only_fold=None, embargo_days=0)
    args.update(kw)
    return WF.make_plan("wfp", folds, ["--expert", "FMPRating", "--strategy", "S2"],
                        "ba2-test", 2, TH, **args)


# ------------------------------------------------------------------ resume


def test_completed_train_job_is_skipped_and_incomplete_one_is_run():
    plan = _plan(only_fold=None)
    is_picks = {f"wfp-wf{k}": [_pick(1), _pick(2)] for k in (1, 2, 3)}
    oos = {(k, r): _oos(10.0, -10.0) for k in (1, 2, 3) for r in (1, 2)}
    r = FakeRunner(completed={"wfp-wf1", "wfp-wf3"}, is_picks=is_picks, oos=oos)
    WF.run_walk_forward(plan, r, log=lambda *_: None)
    assert [n for n, _ in r.trained] == ["wfp-wf2"]
    assert r.trained[0][1] == ["ba2-test", "optimize", "--expert", "FMPRating", "--strategy", "S2",
                               "--start", "2020-01-01", "--end", "2024-12-31", "--name", "wfp-wf2"]
    assert [t[1] for t in r.tested] == [1, 2, 3]
    assert r.tested[0][2:] == ("WF1-OOS-R", ["WalkForward", "wf-fold-1", "OOS"], 1, [1, 2])


def test_train_that_does_not_complete_is_an_error():
    class Bad(FakeRunner):
        def run_train(self, cmd, name):
            self.trained.append((name, cmd))      # never marks completed
    plan = _plan(only_fold=1)
    with pytest.raises(WF.WalkForwardError, match="did not reach"):
        WF.run_walk_forward(plan, Bad(), log=lambda *_: None)


def test_skip_train_requires_completed_job_and_never_trains():
    plan = _plan(skip_train=True, only_fold=1)
    with pytest.raises(WF.WalkForwardError, match="not completed"):
        WF.run_walk_forward(plan, FakeRunner(), log=lambda *_: None)
    r = FakeRunner(completed={"wfp-wf1"}, is_picks={"wfp-wf1": [_pick(1), _pick(2)]},
                   oos={(1, 1): _oos(5, -5), (1, 2): _oos(5, -5)})
    WF.run_walk_forward(plan, r, log=lambda *_: None)
    assert r.trained == []


def test_unknown_only_fold_is_refused():
    with pytest.raises(WF.WalkForwardError, match="only-fold"):
        _plan(only_fold=9)


def test_missing_oos_result_raises():
    plan = _plan(only_fold=1)
    r = FakeRunner(completed={"wfp-wf1"}, is_picks={"wfp-wf1": [_pick(1), _pick(2)]},
                   oos={(1, 1): _oos(5, -5), (1, 2): _oos(5, -5)})
    r.run_oos = lambda *a, **k: {1: _oos(5, -5)}      # rank 2 lost
    with pytest.raises(WF.WalkForwardError, match="rank 2"):
        WF.run_walk_forward(plan, r, log=lambda *_: None)


def test_missing_metric_raises_instead_of_defaulting():
    f = WF.build_year_folds(D("2020-01-01"), [2024])[0]
    with pytest.raises(WF.MissingMetric):
        WF.evaluate_genome(f, _pick(1), WF.OosMetrics(1, None, -5.0, 3), TH)
    with pytest.raises(WF.MissingMetric):
        WF.evaluate_genome(f, _pick(1), WF.OosMetrics(1, 5.0, -5.0, None), TH)


# ------------------------------------------------------------------ aggregation maths


def _fold_2024_2025():
    return WF.build_year_folds(D("2020-01-01"), [2024, 2025])


def test_compounding_worst_dd_trades_and_efficiency_hand_computed():
    assert WF.compound([10.0, -50.0]) == pytest.approx(-45.0)
    assert WF.compound([]) == 0.0
    f1, f2 = _fold_2024_2025()
    # 2024 is a leap year: 366/365.25 y; 2025: 365/365.25 y. Test windows are
    # 2024-01-01..2024-12-31 (365 days) and 2025-01-01..2025-12-31 (364 days) by (end-start).days.
    g1 = WF.evaluate_genome(f1, _pick(1, car=20.0, dd=-10.0), _oos(21.0, -12.0, 7), TH)
    g2 = WF.evaluate_genome(f2, _pick(1, car=30.0, dd=-10.0), _oos(-10.0, -9.0, 5), TH)
    y1, y2 = 365 / 365.25, 364 / 365.25
    assert g1.oos_car == pytest.approx((1.21 ** (1 / y1) - 1) * 100)
    assert g1.efficiency == pytest.approx(g1.oos_car / 20.0)
    s = WF.stitch([(f1, g1), (f2, g2)])
    total = (1.21 * 0.90 - 1) * 100                      # = 8.9
    assert s.total_return == pytest.approx(8.9)
    assert s.years == pytest.approx(y1 + y2)
    assert s.oos_car == pytest.approx(((1 + total / 100) ** (1 / (y1 + y2)) - 1) * 100)
    assert s.worst_dd == -12.0                           # deepest |dd| across folds
    assert s.trades == 12
    assert s.is_car_mean == pytest.approx(25.0)
    assert s.efficiency == pytest.approx(s.oos_car / 25.0)


def test_efficiency_is_undefined_when_is_car_not_positive():
    assert WF.efficiency(10.0, 0.0) is None
    assert WF.efficiency(10.0, -5.0) is None
    assert WF.efficiency(10.0, 20.0) == 0.5


# ------------------------------------------------------------------ verdict


def _result(oos_by_fold, th=TH, planned=None, is_dd=-20.0):
    folds = WF.build_year_folds(D("2020-01-01"), [2024, 2025, 2026])[:len(oos_by_fold)]
    genomes = []
    for f, o in zip(folds, oos_by_fold):
        genomes.append(WF.evaluate_genome(f, _pick(1, dd=is_dd), o, th))
    return WF.aggregate("p", folds, planned or len(folds), 1, genomes, th)


def test_genome_passes_when_all_three_criteria_hold():
    res = _result([_oos(5.0, -25.0), _oos(3.0, -30.0)], planned=2)
    assert [g.passed for g in res.genomes] == [True, True]
    assert res.overall_passed is True and res.winners_passed == 2


def test_return_and_drawdown_failures_are_explained():
    res = _result([_oos(-1.0, -25.0), _oos(5.0, -31.0)], planned=2)   # 31 > 1.5*20
    assert [g.passed for g in res.genomes] == [False, False]
    assert "OOS return" in res.genomes[0].fail_reasons[0]
    assert "maxDD" in res.genomes[1].fail_reasons[0]
    assert res.overall_passed is False


def test_zero_oos_trades_fails_even_with_a_great_return():
    res = _result([_oos(50.0, -1.0, trades=0)], th=WF.Thresholds(0.0, 1.5, 1))
    g = res.genomes[0]
    assert g.passed is False and g.fail_reasons[0] == "zero OOS trades"
    assert res.overall_passed is False


def test_pass_min_folds_boundary():
    oos = [_oos(5.0, -10.0), _oos(-5.0, -10.0), _oos(5.0, -10.0)]
    assert _result(oos, th=WF.Thresholds(0.0, 1.5, 2)).overall_passed is True
    assert _result(oos, th=WF.Thresholds(0.0, 1.5, 3)).overall_passed is False


def test_partial_run_has_no_overall_verdict():
    res = _result([_oos(5.0, -10.0)], planned=3)
    assert res.complete is False and res.overall_passed is None
    assert "PARTIAL" in res.notes[0]
    assert "INCOMPLETE" in WF.render_markdown(res)


def test_ranks_missing_in_a_fold_are_not_stitched():
    folds = _fold_2024_2025()
    gs = [WF.evaluate_genome(folds[0], _pick(1), _oos(5, -5), TH),
          WF.evaluate_genome(folds[0], _pick(2), _oos(5, -5), TH),
          WF.evaluate_genome(folds[1], _pick(1), _oos(5, -5), TH)]
    res = WF.aggregate("p", folds, 2, 2, gs, TH)
    assert sorted(res.rank_stitches) == [1] and "rank 2" in res.notes[0]


def test_report_states_thresholds_and_writes_json_and_markdown(tmp_path):
    res = _result([_oos(5.0, -25.0), _oos(3.0, -30.0)], planned=2)
    jp, mp = WF.write_report(res, str(tmp_path / "p"))
    md = Path(mp).read_text(encoding="utf-8")
    assert "`--pass-min-oos-return` 0%" in md and "`--pass-max-dd-mult` 1.5" in md
    assert "`--pass-min-folds` 2" in md and "Overall verdict: PASS" in md
    assert "zero OOS trades" in md
    data = json.loads(Path(jp).read_text(encoding="utf-8"))
    assert data["thresholds"] == {"min_oos_return": 0.0, "max_dd_mult": 1.5, "min_folds": 2}
    assert data["winner_stitch"]["trades"] == 20 and data["overall_passed"] is True


# ------------------------------------------------------------------ dry run + extension point


def test_dry_run_prints_exact_commands_and_names(capsys):
    tool = _tool()
    rc = tool.main(["--prefix", "wfx", "--train-start", "2020-01-01", "--test-years",
                    "2024,2025,2026", "--test-end", "2026-06-30", "--top-n", "3",
                    "--pass-min-oos-return", "0", "--pass-max-dd-mult", "1.5",
                    "--pass-min-folds", "2", "--launcher", "ba2-test", "--dry-run", "--",
                    "--expert", "FMPRating", "--strategy", "S2", "--universe", "AAPL"],
                   runner=FakeRunner())
    out = capsys.readouterr().out
    assert rc == 0 and "nothing was run" in out
    assert ("ba2-test optimize --expert FMPRating --strategy S2 --universe AAPL --start "
            "2020-01-01 --end 2023-12-31 --name wfx-wf1") in out
    assert "--end 2025-12-31 --name wfx-wf3" in out
    assert "WF3-OOS-R<rank>-wfx-wf3" in out and "2026-01-01..2026-06-30" in out
    assert "OOS return >= 0%" in out


def test_dry_run_runs_nothing():
    tool = _tool()
    r = FakeRunner()
    tool.main(["--prefix", "wfx", "--train-start", "2020-01-01", "--test-years", "2024",
               "--top-n", "1", "--pass-min-oos-return", "0", "--pass-max-dd-mult", "1",
               "--pass-min-folds", "1", "--dry-run", "--", "--expert", "X"], runner=r)
    assert r.trained == [] and r.tested == []


def test_seed_extension_point_is_none_and_never_silently_ignored(monkeypatch):
    f = WF.build_year_folds(D("2020-01-01"), [2024])[0]
    assert WF.initial_population_for_fold(f) is None
    monkeypatch.setattr(WF, "initial_population_for_fold", lambda fold: ["seed"])
    with pytest.raises(NotImplementedError):
        WF.train_command("ba2-test", [], "p", f)


# ------------------------------------------------------------------ the option holdout rail


def _an_option_strategy(L):
    return sorted(L._PURE_OPTION_STRATEGIES)[0]


def test_rail_still_refuses_a_pure_option_fold_whose_train_window_reaches_2026():
    L = _launcher_mod()
    s = _an_option_strategy(L)
    bad = WF.parse_explicit_fold("2020-01-01:2026-03-31:2026-04-01:2026-06-30", 1)
    WF.validate_folds([bad])                       # the engine's own fold maths accepts it ...
    with pytest.raises(SystemExit, match="holdout"):
        L._assert_option_window_excludes_holdout([s], bad.train_end.isoformat())   # ... the rail does not
    # the standard plan never trains into 2026
    for f in WF.build_year_folds(D("2020-01-01"), [2024, 2025, 2026]):
        L._assert_option_window_excludes_holdout([s], f.train_end.isoformat())


def test_stock_strategies_are_not_touched_by_the_rail():
    L = _launcher_mod()
    L._assert_option_window_excludes_holdout(["S2"], "2026-06-30")


def test_rerun_on_2026_goes_through_persist_with_window_and_never_calls_the_rail(monkeypatch):
    """The re-run path (``_persist_top_backtests``) does not consult the optimize rail: it is a
    single measurement. Pin that, and that ``window=`` is the only thing that changes."""
    import app.models  # noqa: F401
    from app.models.backtest import Backtest
    from app.models.database import Base, SessionLocal, engine
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.services import strategy_optimization_handler as soh
    from app.services import sync_client
    Base.metadata.create_all(bind=engine)
    L = _launcher_mod()

    def _boom(*a, **k):
        raise AssertionError("the optimize holdout rail was consulted on the re-run path")
    monkeypatch.setattr(L, "_assert_option_window_excludes_holdout", _boom)
    monkeypatch.setattr(sync_client, "push_backtest", lambda bt, db: None)
    seen = []

    def worker(cfg):
        seen.append(cfg)
        return {"ok": True, "results": {
            "total_trades": 3, "winning_trades": 2, "losing_trades": 1, "win_rate": 66.67,
            "total_return": 5.0, "annualized_return": 12.3, "buy_hold_return": 0.0,
            "sharpe_ratio": 1.1, "sortino_ratio": 1.4, "calmar_ratio": 0.9, "volatility": 8.2,
            "max_drawdown": -4.55, "avg_drawdown": -2.1, "max_drawdown_duration": 1.0,
            "profit_factor": 4.0, "expectancy": 2.0, "sqn": 0.7, "avg_trade": 2.0,
            "best_trade": 5.0, "worst_trade": -2.0, "avg_trade_duration": 2.0,
            "exposure_time": 33.3, "final_equity": 105_000.0, "equity_peak": 110_000.0,
            "equity_curve": [{"date": "2026-01-02", "equity": 100_000.0}],
            "drawdown_curve": [{"date": "2026-01-02", "drawdown": 0.0}], "trades": []}}
    monkeypatch.setattr(soh, "_persist_trial_worker", worker)

    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(L, "_new_local_pool", lambda n: ThreadPoolExecutor(max_workers=n))

    db = SessionLocal()
    try:
        strat = Strategy(name="wf-strat", entry_rules=[], exit_rules=[])
        db.add(strat); db.commit(); db.refresh(strat)
        opt = StrategyOptimization(
            strategy_id=strat.id, name="wf-train", fitness_metric="sharpe",
            optimization_type="genetic",
            optimization_config={"backtest": {
                "backtest_id": 1, "start_date": "2020-01-01", "end_date": "2025-12-31",
                "labels": ["Grid"], "enabled_instruments": ["AAPL"],
                "experts": [{"class": "FMPRating", "settings": {}}], "initial_capital": 10000.0,
                "account_settings": {"starting_cash": 10000.0}, "warmup_days": 30, "seed": 42}},
            all_results=[{"params": {}, "fitness": 1.23, "trades": 10}],
            best_params={}, best_fitness=1.23, status="completed")
        db.add(opt); db.commit(); db.refresh(opt)
        opt_id = opt.id
    finally:
        db.close()

    ids = []
    n = L._persist_top_backtests(
        opt_id, "FMPRating", n=1, parallel=1, candidates=[({}, "k", 1.23)], ranks=[2],
        name_prefix=WF.oos_name_prefix(3), extra_labels=WF.oos_labels(3), use_remote_workers=False,
        persisted_ids=ids, window=("2026-01-01", "2026-06-30"))
    assert n == 1 and ids[0][0] == 2
    assert seen[0]["start_date"] == "2026-01-01" and seen[0]["end_date"] == "2026-06-30"
    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter_by(id=ids[0][1]).one()
        assert bt.name == WF.oos_backtest_name(3, 2, "wf-train") == "WF3-OOS-R2-wf-train"
        assert bt.start_date == datetime(2026, 1, 1) and bt.end_date == datetime(2026, 6, 30)
        assert bt.labels == ["Grid", "WalkForward", "wf-fold-3", "OOS"]
        assert "ga_fitness_divergence" not in (bt.results or {})   # other window: gate skipped
    finally:
        db.close()
