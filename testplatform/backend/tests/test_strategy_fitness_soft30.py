"""BT's explicit discovery objective: penalise fewer than 30 bets, never annualise it."""
import inspect

import pytest

from app.services import strategy_fitness as F
from app.services import strategy_optimization_handler as H


METRIC = "option_car_target_soft30"


def result(count=15, legs=1, **changes):
    trades = [
        {"contract_symbol": f"T{txn}L{leg}", "transaction_id": txn,
         "symbol": "AAPL", "pnl": 100.0 / legs, "pnl_pct": 1.0,
         "entry_price": 1.0, "size": 1.0, "exit_time": "2020-12-30"}
        for txn in range(count) for leg in range(legs)
    ]
    values = {
        "total_trades": len(trades), "trades": trades,
        "avg_trades_per_year": len(trades) / 6,
        "annualized_return": 50.0, "max_drawdown": -30.0,
        "car_hard_min_trades_per_year": 8.0, "car_min_trades_per_year": 20.0,
        "equity_curve": [{"date": "2020-01-01", "equity": 10000.0},
                         {"date": "2020-12-31", "equity": 15000.0}],
    }
    values.update(changes)
    return values


@pytest.mark.parametrize("count,factor", [(1, 1/30), (5, 1/6), (15, .5), (29, 29/30),
                                           (30, 1), (60, 1)])
@pytest.mark.parametrize("legs", [1, 2, 4])
def test_positive_count_has_a_soft_total_structure_penalty(count, factor, legs):
    assert F.compute_fitness(METRIC, result(count, legs)) == pytest.approx(50 * factor)


def test_year_rate_and_expert_floors_do_not_change_the_total_count_policy():
    r = result(15, avg_trades_per_year=.01, car_hard_min_trades_per_year=1000,
               car_min_trades_per_year=2000)
    assert F.compute_fitness(METRIC, r) == 25
    assert F.compute_fitness("option_car_target", r) == F.LOW_TRADE_SENTINEL


def test_no_trades_and_wipeout_remain_disqualified():
    assert F.compute_fitness(METRIC, result(0)) == F.ZERO_TRADE_SENTINEL
    assert F.compute_fitness(METRIC, result(1, max_drawdown=-100)) == F.WIPED_OUT_SENTINEL
    assert F.compute_fitness(METRIC, result(0, account_wiped_out=True)) == F.WIPED_OUT_SENTINEL


def test_do_not_reward_a_thin_losing_book_by_multiplying_its_loss():
    assert F.compute_fitness(METRIC, result(1, annualized_return=-20)) == -20


@pytest.mark.parametrize("missing", ["trades", "equity_curve", "max_drawdown"])
def test_rescoring_requires_actual_structure_and_equity_evidence(missing):
    r = result()
    del r[missing]
    with pytest.raises(ValueError, match=missing):
        F.compute_fitness(METRIC, r)


def test_preserves_cap_aware_return_and_optional_wrappers():
    capped = result(profit_cap_pct=2000, adjusted_annualized_return=35)
    assert F.compute_fitness(METRIC, capped) == 17.5
    assert F.compute_fitness(METRIC, result(fitness_win_rate_factor=True, win_rate=25)) == 12.5
    # A second trade-count penalty is not applied on top of the built-in ramp.
    assert F.compute_fitness(METRIC, result(fitness_trade_scale=True)) == 25


def test_robustness_still_penalises_concentration_without_a_count_sentinel():
    r = result(15)
    raw = F.compute_fitness(METRIC, r, robust=False)
    assert F.compute_fitness(METRIC, r, robust=True) == raw
    r["trades"][0]["pnl"] = 1_000_000
    adjusted = F.compute_fitness(METRIC, r, robust=True)
    assert 0 <= adjusted < raw
    assert F.compute_fitness(METRIC, result(5), robust=True) == 0


def test_catalog_and_error_expose_the_distinct_objective():
    F.assert_catalog_complete()
    assert METRIC in F.catalog_accepted_metrics()
    assert F.compute_fitness(METRIC.upper(), result()) == 25
    with pytest.raises(ValueError, match=METRIC):
        F.compute_fitness("unknown", result())


@pytest.mark.parametrize("old,new", [(None, METRIC), ("option_car_target", METRIC),
                                     (METRIC, "option_car_target")])
def test_changed_objective_refuses_old_checkpoint_scores(old, new):
    ckpt = {} if old is None else {"fitness_metric": old}
    with pytest.raises(ValueError, match="NEW name"):
        H._assert_checkpoint_metric_matches(ckpt, new, "test-job", "test-ckpt")


def test_matching_and_legacy_checkpoints_keep_working():
    H._assert_checkpoint_metric_matches({"fitness_metric": METRIC}, METRIC, "job", "id")
    H._assert_checkpoint_metric_matches({}, "option_car_target", "job", "id")
    H._assert_checkpoint_metric_matches({"fitness_metric": "ocar"},
                                       "option_consistent_annual_return", "job", "id")
    src = inspect.getsource(H.handle_strategy_optimization)
    assert 'data["fitness_metric"] = opt.fitness_metric' in src
    assert src.index("_assert_checkpoint_metric_matches(ckpt") < src.index("optimizer.resume_from_checkpoint(ckpt)")


def test_matrix_forwards_metric_and_uses_a_different_discovery_identity():
    from tests.test_option_discovery_driver import M, discovery_args, name_for

    args = discovery_args("--fitness", METRIC)
    cmd = M.build_cmd(args, "launcher.py", "job", "DeterministicScorer", "O_LC", "AAPL")
    assert cmd[cmd.index("--fitness") + 1] == METRIC
    assert name_for(args) != name_for(discovery_args("--fitness", "option_car_target"))


def test_launcher_persists_metric_with_robustness_on(monkeypatch):
    from tests.test_robust_fitness_default_on import _parse, _run_optimize, _BASE_ARGV

    config = _run_optimize(_parse(_BASE_ARGV + ["--fitness", METRIC]), monkeypatch)
    # The metric travels on StrategyOptimization itself; this helper returns its config.
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization
    with SessionLocal() as db:
        row = db.query(StrategyOptimization).order_by(StrategyOptimization.id.desc()).first()
        assert row.fitness_metric == METRIC
    assert config["backtest"]["robust_fitness"] is True


# ---------------------------------------------------------------------------------------------
# A 1-trade genome is 100% concentrated (plan 2026-09-24 Task 4)
# ---------------------------------------------------------------------------------------------
# robustness_metrics used to return every factor 1.0 below 2 trades, so a single winning trade
# was scored as PERFECTLY diversified while a 2-5-trade book got concentration factor 0. Under
# soft30 (which has no count floor, only a 1/30-per-bet ramp) that made every top O_LP genome a
# 1-trade genome. Thin trading is penalised, not zeroed by a new floor: the one trade simply
# goes through the same concentration formula as every other run.

def test_a_single_winning_trade_is_fully_concentrated():
    r = result(1)
    F.compute_fitness(METRIC, r, robust=True)
    comp = r["robustness"]
    assert comp["top1_pct"] == 100.0
    assert comp["top5_pct"] == 100.0
    assert comp["conc_factor"] == 0.0
    # A trade-ORDER resample of one trade has nothing to reorder; the factor is left neutral
    # rather than fabricated -- concentration is the screen that speaks here.
    assert comp["mc_factor"] == 1.0


def test_one_trade_never_outscores_two_under_soft30():
    one = F.compute_fitness(METRIC, result(1), robust=True)
    two = F.compute_fitness(METRIC, result(2), robust=True)
    assert one <= two


def test_a_single_losing_trade_keeps_the_early_return():
    r = result(1)
    r["trades"][0]["pnl"] = -100.0
    F.compute_fitness(METRIC, r, robust=True)
    assert r["robustness"]["top1_pct"] is None
    assert r["robustness"]["conc_factor"] == 1.0


@pytest.mark.parametrize("metric", ["calmar_ratio", "total_return", "sharpe_ratio",
                                    "consistent_annual_return"])
def test_equity_metrics_keep_the_single_trade_early_return(metric):
    # The equity path is frozen bit-for-bit (test_strategy_fitness_equity_frozen). The
    # generic metrics have no trade floor, so a 1-trade equity run DOES reach the robustness
    # screen; the change is therefore gated to the option CAR-family metric names.
    r = result(1, calmar_ratio=2.0, total_return=5.0, sharpe_ratio=1.0)
    F.compute_fitness(metric, r, robust=True)
    comp = r["robustness"]
    assert comp["top1_pct"] is None
    assert comp["conc_factor"] == 1.0


# ---------------------------------------------------------------------------------------------
# Option metrics score concentration on STRUCTURES, not legs
# ---------------------------------------------------------------------------------------------
# Rows are legs, so on the row list one iron condor is 4 "trades", any run with <= 5 rows reads
# top5 = 100%, and offsetting legs push top1 past 100%. The soft30 ramp already counts
# structures; the screen must too.

def structures(legs_per_structure, **changes):
    """A run from explicit per-structure leg P&Ls: [[leg, leg, ...], ...]; pnl_pct = pnl / 1000."""
    trades = [{"contract_symbol": f"T{txn}L{k}", "transaction_id": txn, "symbol": "AAPL",
               "pnl": p, "pnl_pct": p / 1000.0, "entry_price": 1.0, "size": 1.0,
               "exit_time": "2020-12-30"}
              for txn, legs in enumerate(legs_per_structure) for k, p in enumerate(legs)]
    return result(0, **{"total_trades": len(trades), "trades": trades,
                        "avg_trades_per_year": len(trades) / 6, **changes})


def comp_for(metric, run):
    F.compute_fitness(metric, run, robust=True)
    return run["robustness"]


@pytest.mark.parametrize("metric", ["option_consistent_annual_return", "option_car", "ocar",
                                    "option_car_over_risk", "option_car_target", METRIC,
                                    "Option_Car_Target_SOFT30", "OCAR"])
def test_every_option_metric_name_scores_structures(metric):
    # The hard-floor option metrics return LOW_TRADE_SENTINEL for one trade, but the screen's
    # components are still recorded -- which is what shows the flag reached it.
    comp = comp_for(metric, structures([[100.0]]))
    assert (comp["top1_pct"], comp["top5_pct"], comp["conc_factor"]) == (100.0, 100.0, 0.0)


def test_one_iron_condor_is_concentrated_exactly_like_one_single_leg_trade():
    condor = structures([[300.0, -100.0, 200.0, -50.0]])
    single = structures([[350.0]])
    condor_fit = F.compute_fitness(METRIC, condor, robust=True)
    single_fit = F.compute_fitness(METRIC, single, robust=True)
    assert condor["robustness"] == single["robustness"]
    # On legs the condor read top1 = 300/350 = 85.7% and resampled legs apart; on the one bet it
    # is 100% with a neutral MC, like any single trade.
    assert condor["robustness"]["top1_pct"] == 100.0
    assert condor["robustness"]["mc_factor"] == 1.0
    assert condor_fit == single_fit == 0.0


def test_six_structures_concentration_is_computed_on_structures():
    # Structures: one vertical netting 400 + 100 = 500, then five single legs of 100.
    # net = 1000; top1 = 500/1000 = 50%; top5 = (500 + 4 x 100)/1000 = 90%;
    # factor = ((100 - 90) / (100 - 40)) ** 1.5 = (1/6) ** 1.5.
    # (On the 7 LEGS it would read top1 = 40%, top5 = 80%, factor (1/3) ** 1.5.)
    comp = comp_for(METRIC, structures([[400.0, 100.0]] + [[100.0]] * 5))
    assert comp["top1_pct"] == pytest.approx(50.0)
    assert comp["top5_pct"] == pytest.approx(90.0)
    assert comp["conc_factor"] == pytest.approx((1.0 / 6.0) ** 1.5)


@pytest.mark.parametrize("book", [
    [[1664.1432316152855]],                                          # 100*x/x == 99.99999999999999
    [[77.28734524697666], [428.9689436710173], [-412.49148439023816]],  # sorted-sum reorder
], ids=["one_structure", "three_structures"])
def test_a_by_definition_100pct_share_is_exactly_100(book):
    """Float division read these as 99.99999999999999 -> factor ~3.6e-24, ranking a thin genome
    above an exact 0. Where the share is 100% by definition it is set, not divided."""
    comp = comp_for(METRIC, structures(book))
    assert comp["top5_pct"] == 100.0
    assert comp["conc_factor"] == 0.0
    if len(book) == 1:
        assert comp["top1_pct"] == 100.0


def test_monte_carlo_resamples_structures_not_legs():
    # 20 verticals, each +100 / -90 on its two legs: every BET wins +10, so no reordering of
    # bets can lose. Resampling the LEGS independently splits the hedge and loses often.
    run = structures([[100.0, -90.0]] * 20, max_drawdown=-5.0)
    comp = comp_for(METRIC, run)
    assert comp["mc_prob_neg"] == 0.0
    assert comp["mc_factor"] == 1.0
    legs = F.robustness_metrics(run)          # equity/row view of the same list, for contrast
    assert legs["mc_prob_neg"] > 0.0


# ---------------------------------------------------------------------------------------------
# Share lots and the overlays written against them are ONE bet (O_CC / O_PP / O_WHEEL)
# ---------------------------------------------------------------------------------------------
# The order code books an overlay as its own transaction and assigned stock as a new equity
# transaction, so the transaction partition split one position in two whose legs offset each
# other. The option metrics join them (FITNESS grouping only; order code unchanged).

def _share(k, t_in, t_out, pnl, px=150.0, txn=None):
    return {"symbol": "AAPL", "transaction_id": 100 + k if txn is None else txn, "pnl": pnl,
            "pnl_pct": pnl / 1000.0, "entry_time": t_in, "exit_time": t_out,
            "entry_price": px, "size": 100.0, "exit_reason": "take_profit"}


def _leg(k, t_in, t_out, pnl, kind="P", strike=138.0, reason="closed"):
    return {"symbol": "AAPL", "underlying_symbol": "AAPL", "contract_symbol": f"AAPL{kind}{k}",
            "transaction_id": 200 + k, "pnl": pnl, "pnl_pct": pnl / 1000.0, "strike": strike,
            "entry_time": t_in, "exit_time": t_out, "entry_price": 2.0, "size": 1.0,
            "exit_reason": reason}


def _o_pp(same_bar=False):
    """12 protective-put cycles: shares +1000, put -600, each cycle its own two transactions."""
    rows = []
    for k in range(12):
        m = k + 1
        if same_bar:   # sell both and buy both again on the SAME bar
            t_in = f"2020-{m:02d}-01T14:30:00"
            t_out = f"2020-{m + 1:02d}-01T14:30:00" if m < 12 else "2021-01-01T14:30:00"
            p_in = t_in
        else:
            t_in, t_out, p_in = (f"2020-{m:02d}-01T14:30:00", f"2020-{m:02d}-25T14:30:00",
                                 f"2020-{m:02d}-02T14:30:00")
        rows += [_share(k, t_in, t_out, 1000.0), _leg(k, p_in, t_out, -600.0)]
    return rows


@pytest.mark.parametrize("same_bar", [False, True], ids=["gap", "same_bar_reentry"])
def test_protective_put_cycles_are_scored_as_twelve_combined_bets(same_bar):
    rows = _o_pp(same_bar)
    run = result(0, total_trades=len(rows), trades=rows, avg_trades_per_year=len(rows))
    fit = F.compute_fitness(METRIC, run, robust=True)
    comp = run["robustness"]
    # 12 bets of +400: top1 = 400/4800, top5 = 2000/4800 = 41.67%. Re-opening on the bar the
    # previous cycle closed must NOT chain the cycles into one bet.
    assert F._structure_count(rows, option_structures=True) == 12
    assert comp["top1_pct"] == pytest.approx(100 / 12)
    assert comp["top5_pct"] == pytest.approx(500 / 12)
    expected_conc = ((100 - 500 / 12) / 60) ** 1.5
    assert comp["conc_factor"] == pytest.approx(expected_conc)
    # The soft30 ramp counts the same 12 bets the screen scored.
    assert fit == pytest.approx(50 * (12 / 30) * expected_conc)
    # Default (equity / convex) partition is untouched: 24 separate transactions.
    assert F._structure_count(rows) == 24
    assert len(F._structure_pnls(rows)) == 24


def test_a_wheel_cycle_is_one_bet():
    """CSP -> assigned at the strike -> two covered calls -> called away: one share-holding
    window, one bet, even though the order code books four transactions."""
    rows = [
        _leg(0, "2021-01-04T14:30:00", "2021-01-15T21:00:00", 150.0, strike=100.0,
             reason="assigned"),
        _share(0, "2021-01-15T21:00:00", "2021-03-19T21:00:00", 400.0, px=100.0),
        _leg(1, "2021-01-19T14:30:00", "2021-02-19T21:00:00", 120.0, kind="C", strike=105.0,
             reason="expired_otm"),
        _leg(2, "2021-02-22T14:30:00", "2021-03-19T21:00:00", -80.0, kind="C", strike=104.0,
             reason="assigned"),
    ]
    assert F._structure_count(rows, option_structures=True) == 1
    assert F._structure_pnls(rows, option_structures=True) == [pytest.approx(590.0)]
    assert F._structure_count(rows) == 4


def test_a_same_bar_market_buy_is_not_mistaken_for_an_assignment():
    """The put settled at the strike (100) on the bar a new lot was BOUGHT at the market (97.5):
    that is a new position, not the put's delivery."""
    rows = [_leg(0, "2021-01-04T14:30:00", "2021-01-15T21:00:00", 150.0, strike=100.0,
                 reason="exercised"),
            _share(0, "2021-01-15T21:00:00", "2021-02-19T21:00:00", 400.0, px=97.5)]
    assert F._structure_count(rows, option_structures=True) == 2


def test_overlapping_share_lots_alone_are_never_joined():
    """Only a share lot and an OPTION structure join; an equity-only list keeps one bet per
    row under the option partition too."""
    rows = [_share(0, "2021-01-04T14:30:00", "2021-03-01T21:00:00", 100.0),
            _share(1, "2021-02-01T14:30:00", "2021-04-01T21:00:00", 200.0)]
    assert F._structure_count(rows, option_structures=True) == 2


@pytest.mark.parametrize("metric", sorted(F.catalog_accepted_metrics()))
def test_scores_option_structures_matches_compute_fitness(metric):
    """The deploy-time concentration check (tools/genome_concentration_check.py) groups trades
    by ``scores_option_structures``; it must name exactly the metrics whose compute_fitness
    branch screens on option structures, or the check disagrees with what the GA ranked."""
    for spelling in (metric, metric.upper()):
        run = result(1, calmar_ratio=2.0, total_return=5.0, sharpe_ratio=1.0,
                     sortino_ratio=1.0, profit_factor=1.5, sqn=1.0, win_rate=100.0)
        F.compute_fitness(spelling, run, robust=True)
        comp = run.get("robustness") or {}
        screened_on_structures = comp.get("top1_pct") == 100.0
        assert F.scores_option_structures(spelling) is screened_on_structures, spelling
