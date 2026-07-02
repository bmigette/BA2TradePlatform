# tests/backtest/test_monte_carlo.py
import numpy as np
from app.services.backtest.monte_carlo import (
    equity_path_from_trade_pcts, mc_bootstrap, mc_shuffle, drop_k_best, summarize_paths,
)

def _trades(pcts):
    return [{"pnl_pct": p, "exit_time": f"2023-0{1+i%9}-15T00:00:00"} for i, p in enumerate(pcts)]

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
