"""Map StrategyOptimization.fitness_metric -> a scalar from the BACKTEST results dict.

GA is maximize-only (FitnessMax weights=(1.0,)), so max_drawdown is negated.
0-trade configs return a sentinel LARGE-NEGATIVE fitness distinct from the 0.0
exception fallback in GeneticOptimizer.optimize (genetic.py: the evaluate wrapper
returns (0.0,) when fitness_function raises). Keeping the no-trade sentinel
distinct from 0.0 means a no-trade config is never confused with a crashed trial,
and is always worse than any real config.

The results-dict keys are those produced by the Phase-2 daily backtest runner
(results.build_results / _compute_metrics) which are byte-compatible with
_convert_bt_results: total_trades, sharpe_ratio, total_return, profit_factor,
win_rate, sortino_ratio, calmar_ratio, sqn, max_drawdown (all confirmed present).
"""
import math

# Distinct from 0.0 (the exception fallback) so a no-trade config is never
# confused with a crashed trial, and is always worse than any real config.
ZERO_TRADE_SENTINEL = -1.0e9

# fitness_metric (lower-cased) -> results-dict key. max_drawdown is handled
# specially (negated) and is therefore NOT in this map.
_FITNESS_KEYS = {
    "sharpe": "sharpe_ratio",
    "sharpe_ratio": "sharpe_ratio",
    "return": "total_return",
    "total_return": "total_return",
    "profit_factor": "profit_factor",
    "win_rate": "win_rate",
    "sortino": "sortino_ratio",
    "sortino_ratio": "sortino_ratio",
    "calmar": "calmar_ratio",
    "calmar_ratio": "calmar_ratio",
    "sqn": "sqn",
    # max_drawdown handled specially (negated)
}


def compute_fitness(fitness_metric: str, results: dict) -> float:
    """Return the scalar fitness for a metric from a backtest results dict.

    - None results or 0-trade runs return ZERO_TRADE_SENTINEL (distinct from 0.0).
    - max_drawdown/max_dd/drawdown is NEGATED (smaller drawdown -> larger fitness).
    - NaN/inf metric values collapse to ZERO_TRADE_SENTINEL (degenerate trial).
    - An unknown fitness_metric raises ValueError (no-defaults, fail-early).
    """
    if results is None:
        return ZERO_TRADE_SENTINEL
    if int(results.get("total_trades", 0) or 0) == 0:
        return ZERO_TRADE_SENTINEL

    metric = fitness_metric.lower()
    if metric in ("max_drawdown", "max_dd", "drawdown"):
        dd = results.get("max_drawdown")
        if dd is None:
            return ZERO_TRADE_SENTINEL
        return -float(dd)  # smaller drawdown -> larger (less negative) fitness

    key = _FITNESS_KEYS.get(metric)
    if key is None:
        raise ValueError(
            f"Unknown fitness_metric: {fitness_metric!r}. "
            f"Valid: {sorted(set(_FITNESS_KEYS) | {'max_drawdown'})}"
        )
    # Profit-cap-aware: when a per-trade cap was applied (profit_cap_pct set), the GA must rank on
    # the ADJUSTED return-based metric so one lucky, non-reproducible mega-winner can't win the
    # search. Only return-based metrics have an adjusted variant; the rest fall back to raw.
    if results.get("profit_cap_pct"):
        adj_key = {"calmar_ratio": "adjusted_calmar_ratio",
                   "total_return": "adjusted_total_return",
                   "profit_factor": "adjusted_profit_factor",
                   "sqn": "adjusted_sqn"}.get(key)
        if adj_key is not None and results.get(adj_key) is not None:
            key = adj_key
    # NOTE: sharpe_ratio / sortino_ratio have no adjusted variant yet (they need an adjusted equity
    # curve, not just capped trade pnls), so they fall back to raw even under a cap.
    val = results.get(key)
    if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
        return ZERO_TRADE_SENTINEL
    return float(val)
