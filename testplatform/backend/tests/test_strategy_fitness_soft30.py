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
