# tests/backtest/test_monte_carlo.py
import numpy as np
from app.services.backtest.monte_carlo import (
    equity_path_from_trade_pcts, mc_bootstrap, mc_shuffle, drop_k_best, summarize_paths,
    apply_spread_cost, spread_sweep,
)

def _trades(pcts):
    return [{"pnl_pct": p, "exit_time": f"2023-0{1+i%9}-15T00:00:00"} for i, p in enumerate(pcts)]

def _trades_priced(rows):
    """rows: list of (pnl_pct, entry_price, size) tuples."""
    return [{"pnl_pct": p, "entry_price": ep, "size": sz,
             "exit_time": f"2023-0{1+i%9}-15T00:00:00"}
            for i, (p, ep, sz) in enumerate(rows)]

def test_equity_path_compounds_equity_relative_pcts():
    path = equity_path_from_trade_pcts([10.0, -10.0], initial=10_000.0)
    assert abs(path[-1] - 9_900.0) < 1e-6

def test_shuffle_preserves_total_return_but_not_dd():
    pcts = [5.0, -3.0, 8.0, -6.0, 4.0] * 10
    r = mc_shuffle(pcts, initial=10_000.0, n_paths=200, seed=7)
    finals = {round(p["final_equity"], 4) for p in r}
    assert len(finals) == 1
    dds = {round(p["max_drawdown"], 2) for p in r}
    assert len(dds) > 1

def test_bootstrap_is_seeded_deterministic():
    pcts = [5.0, -3.0, 8.0]
    a = mc_bootstrap(pcts, initial=10_000.0, n_paths=50, seed=42)
    b = mc_bootstrap(pcts, initial=10_000.0, n_paths=50, seed=42)
    assert [p["final_equity"] for p in a] == [p["final_equity"] for p in b]

def test_drop_k_best_removes_top_profit_trades():
    trades = _trades([30.0, 2.0, -1.0, 5.0])
    out = drop_k_best(trades, k=1, initial=10_000.0, years=3.0)
    assert out["dropped"] == [30.0]
    assert out["annualized_return"] < 10.0

def test_summarize_paths_percentiles_and_probs():
    paths = [{"annualized_return": r, "max_drawdown": -d, "calmar": 1.0}
             for r, d in [(10, 5), (20, 10), (30, 15), (40, 25), (50, 30)]]
    s = summarize_paths(paths, target_annual=30.0, dd_limit=20.0)
    assert s["annualized_return"]["p50"] == 30
    assert abs(s["prob_target_annual"] - 0.6) < 1e-9
    assert abs(s["prob_dd_breach"] - 0.4) < 1e-9

def test_apply_spread_cost_deducts_round_trip_bps_from_pnl_pct():
    # Single trade: $10,000 equity, notional = 100 * 100 = $10,000 (fully invested),
    # so round-trip cost at 10bps = 10_000 * 0.0010 * 2 = $20 = 0.20% of the $10,000 entry equity.
    trades = _trades_priced([(5.0, 100.0, 100.0)])
    adjusted = apply_spread_cost(trades, initial=10_000.0, spread_bps=10.0)
    assert len(adjusted) == 1
    assert abs(adjusted[0] - (5.0 - 0.20)) < 1e-6

def test_apply_spread_cost_zero_bps_is_a_noop():
    trades = _trades_priced([(5.0, 100.0, 100.0), (-3.0, 50.0, 40.0)])
    adjusted = apply_spread_cost(trades, initial=10_000.0, spread_bps=0.0)
    assert adjusted == [5.0, -3.0]

def test_apply_spread_cost_tolerates_missing_notional_fields():
    trades = [{"pnl_pct": 5.0, "exit_time": "2023-01-15T00:00:00"}]  # no entry_price/size
    adjusted = apply_spread_cost(trades, initial=10_000.0, spread_bps=10.0)
    assert adjusted == [5.0]  # zero notional -> zero deduction, no crash

def test_apply_spread_cost_multi_trade_uses_pre_trade_equity_not_post_trade():
    # Same 2-trade scenario as the no-op test above, but with a NONZERO spread_bps so both
    # trades' deductions are exercised (trade 0's equity-at-entry is the flat `initial`; trade
    # 1's equity-at-entry must come from the RAW (pre-spread) equity path, i.e. path[1] built
    # from trade 0's un-adjusted 5.0% -- NOT path[2], and NOT a path rebuilt from adjusted pcts).
    # path = equity_path_from_trade_pcts([5.0, -3.0], 10_000.0) = [10000, 10500, 10185].
    # Trade 0: notional = 100*100 = 10_000, equity_at_entry = path[0] = 10_000.
    #   round_trip_cost = 10_000 * 0.0010 * 2 = 20 -> deduct 20/10_000*100 = 0.20 -> 5.0 - 0.20 = 4.8
    # Trade 1: notional = 50*40 = 2_000, equity_at_entry = path[1] = 10_500 (from trade 0's RAW pct).
    #   round_trip_cost = 2_000 * 0.0010 * 2 = 4 -> deduct 4/10_500*100 = 0.0380952... ->
    #   -3.0 - 0.0380952... = -3.038095238095238
    trades = _trades_priced([(5.0, 100.0, 100.0), (-3.0, 50.0, 40.0)])
    adjusted = apply_spread_cost(trades, initial=10_000.0, spread_bps=10.0)
    assert len(adjusted) == 2
    assert abs(adjusted[0] - 4.8) < 1e-9
    assert abs(adjusted[1] - (-3.038095238095238)) < 1e-9

def test_spread_sweep_returns_one_row_per_level_with_monotonic_degradation():
    # A run of alternating small wins/losses, priced so notional ~= equity (spread bites).
    trades = _trades_priced([(3.0, 100.0, 100.0), (-1.0, 100.0, 100.0)] * 20)
    rows = spread_sweep(trades, initial=10_000.0, years=3.0, spread_bps_list=[0, 10, 50])
    assert [r["spread_bps"] for r in rows] == [0, 10, 50]
    # Wider spread -> strictly worse annualized_return (this scenario is built so spread bites
    # every trade, so a no-op implementation that ignores `bps` would produce identical values
    # here and fail this strict chain).
    ann = [r["annualized_return"] for r in rows]
    assert ann[0] > ann[1] > ann[2]

def test_spread_sweep_empty_list_returns_empty():
    trades = _trades_priced([(3.0, 100.0, 100.0)])
    assert spread_sweep(trades, initial=10_000.0, years=3.0, spread_bps_list=[]) == []

def test_run_monte_carlo_spread_bps_haircuts_bootstrap_and_drop_k():
    from app.services.backtest.monte_carlo import run_monte_carlo
    trades = _trades_priced([(5.0, 100.0, 100.0), (-2.0, 100.0, 100.0), (4.0, 100.0, 100.0)] * 5)
    cfg_no_spread = {"methods": ["bootstrap"], "n_paths": 200, "seed": 1, "drop_k": [1]}
    cfg_spread = {**cfg_no_spread, "spread_bps": 20.0}
    r0 = run_monte_carlo(trades, initial=10_000.0, years=3.0, cfg=cfg_no_spread)
    r1 = run_monte_carlo(trades, initial=10_000.0, years=3.0, cfg=cfg_spread)
    # Same seed/paths -> spread strictly worsens the median annualized return.
    assert r1["methods"]["bootstrap"]["annualized_return"]["p50"] < r0["methods"]["bootstrap"]["annualized_return"]["p50"]
    # drop_k_best also runs over the spread-adjusted trades now.
    assert r1["drop_k"][0]["annualized_return"] < r0["drop_k"][0]["annualized_return"]

def test_run_monte_carlo_spread_bps_defaults_to_zero_noop():
    from app.services.backtest.monte_carlo import run_monte_carlo
    trades = _trades_priced([(5.0, 100.0, 100.0)] * 10)
    cfg = {"methods": ["bootstrap"], "n_paths": 50, "seed": 1, "drop_k": []}
    r = run_monte_carlo(trades, initial=10_000.0, years=3.0, cfg=cfg)  # no spread_bps key at all
    assert r["methods"]["bootstrap"]["n_paths"] == 50  # ran fine, no KeyError
