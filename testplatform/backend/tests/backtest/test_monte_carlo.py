# tests/backtest/test_monte_carlo.py
import numpy as np
from app.services.backtest.monte_carlo import (
    equity_path_from_trade_pcts, mc_bootstrap, mc_shuffle, drop_k_best, summarize_paths,
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
    from app.services.backtest.monte_carlo import apply_spread_cost
    # Single trade: $10,000 equity, notional = 100 * 100 = $10,000 (fully invested),
    # so round-trip cost at 10bps = 10_000 * 0.0010 * 2 = $20 = 0.20% of the $10,000 entry equity.
    trades = _trades_priced([(5.0, 100.0, 100.0)])
    adjusted = apply_spread_cost(trades, initial=10_000.0, spread_bps=10.0)
    assert len(adjusted) == 1
    assert abs(adjusted[0] - (5.0 - 0.20)) < 1e-6

def test_apply_spread_cost_zero_bps_is_a_noop():
    from app.services.backtest.monte_carlo import apply_spread_cost
    trades = _trades_priced([(5.0, 100.0, 100.0), (-3.0, 50.0, 40.0)])
    adjusted = apply_spread_cost(trades, initial=10_000.0, spread_bps=0.0)
    assert adjusted == [5.0, -3.0]

def test_apply_spread_cost_tolerates_missing_notional_fields():
    from app.services.backtest.monte_carlo import apply_spread_cost
    trades = [{"pnl_pct": 5.0, "exit_time": "2023-01-15T00:00:00"}]  # no entry_price/size
    adjusted = apply_spread_cost(trades, initial=10_000.0, spread_bps=10.0)
    assert adjusted == [5.0]  # zero notional -> zero deduction, no crash
