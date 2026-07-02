"""Trade-level Monte Carlo core for backtest robustness (Task 1).

PURE functions over a persisted backtest's trade list — NO DB, NO IO, numpy only. The caller
(``robustness_handler``) loads a ``Backtest`` row, extracts ``trades`` / ``initial_capital`` /
run dates, and passes the equity-relative per-trade percentages here.

Equity model — an APPROXIMATION, documented deliberately
--------------------------------------------------------
Each trade's ``pnl_pct`` is stored EQUITY-RELATIVE (pnl / account equity at entry — see
``results.py`` and the plan). We rebuild a synthetic equity curve by applying each trade's pct
SEQUENTIALLY and multiplicatively (``equity *= 1 + pct/100`` per trade), i.e. we assume trades
are NON-overlapping and closed one after another. Real runs hold multiple positions
concurrently, so the true equity path differs; this compounding proxy is intentionally an
approximation good enough to RANK robustness (is the curve luck?), not to reproduce the engine's
exact equity to the cent. The final compounded equity is invariant to trade ORDER (commutative
product) while the intermediate drawdown is path-dependent — which is exactly what the
shuffle/bootstrap/drop-K tests exploit.

Metric conventions are mirrored from ``app/services/backtest/results.py`` so MC numbers line up
with the engine's:
  * annualized_return = ((final / initial) ** (1 / years) - 1) * 100   (guarded when
    initial/final <= 0 or years <= 0 -> 0.0), matching ``results._annualized_return``.
  * max_drawdown = min over the running-peak drawdown series ``(equity - peak) / peak * 100``
    (<= 0, a NEGATIVE pct), matching ``results._drawdown_curve`` + ``max_drawdown = min(dd)``.
  * calmar = annualized_return / abs(max_drawdown)  (0.0 when max_drawdown == 0), matching
    ``results`` ``calmar = annualized_return / abs(max_drawdown) if max_drawdown else 0.0``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np


# ---------------------------------------------------------------------------
# Equity path
# ---------------------------------------------------------------------------
def equity_path_from_trade_pcts(pcts, initial: float) -> np.ndarray:
    """Sequential multiplicative compounding of EQUITY-RELATIVE trade percentages.

    Each ``pct`` is a percent of ACCOUNT equity (e.g. ``10.0`` -> x1.10). Trades are applied one
    after another (``equity *= 1 + pct/100``); this assumes NON-overlapping positions and is an
    approximation of the engine's overlapping-position equity path (see module docstring).

    Returns the equity path INCLUDING the initial point, so ``len == len(pcts) + 1`` and
    ``path[0] == initial``. Order-invariant final value (commutative product); the intermediate
    shape (hence drawdown) IS order-dependent.
    """
    arr = np.asarray(list(pcts), dtype=float)
    factors = 1.0 + arr / 100.0
    path = np.empty(arr.size + 1, dtype=float)
    path[0] = float(initial)
    if arr.size:
        path[1:] = float(initial) * np.cumprod(factors)
    return path


# ---------------------------------------------------------------------------
# Path metrics (mirror results.py conventions)
# ---------------------------------------------------------------------------
def _path_metrics(path: np.ndarray, initial: float, years: float) -> Dict[str, float]:
    """Metrics for one synthetic equity path, mirroring ``results.py`` formulas.

    Returns ``{final_equity, annualized_return, max_drawdown, calmar}`` where max_drawdown is a
    NEGATIVE pct (running-peak drawdown), annualized_return is geometric %/yr, and calmar is
    annualized_return / abs(max_drawdown) (0.0 when there is no drawdown).
    """
    final = float(path[-1]) if path.size else float(initial)

    # Running-peak drawdown series (<= 0), mirrors results._drawdown_curve.
    peaks = np.maximum.accumulate(path)
    # Guard against zero/negative peaks (degenerate blow-up) — treat as no drawdown for that point.
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peaks > 0, (path - peaks) / peaks * 100.0, 0.0)
    max_drawdown = float(dd.min()) if dd.size else 0.0  # most negative

    annualized_return = _annualized_return(initial, final, years)
    calmar = (annualized_return / abs(max_drawdown)) if max_drawdown else 0.0

    return {
        "final_equity": final,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
    }


def _annualized_return(initial: float, final: float, years: float) -> float:
    """Geometric annualised return (%). Mirrors ``results._annualized_return`` exactly."""
    if initial <= 0 or final <= 0 or years <= 0:
        return 0.0
    return ((final / initial) ** (1.0 / years) - 1.0) * 100.0


# ---------------------------------------------------------------------------
# Monte Carlo methods
# ---------------------------------------------------------------------------
def mc_bootstrap(pcts, initial: float, n_paths: int, seed: int, years: float = 3.0) -> List[Dict[str, float]]:
    """Bootstrap: per path, resample ``len(pcts)`` trades WITH replacement, compute path metrics.

    Deterministic given ``seed`` (``np.random.default_rng(seed)``). Returns a list of the
    per-path metric dicts from ``_path_metrics``.
    """
    arr = np.asarray(list(pcts), dtype=float)
    rng = np.random.default_rng(seed)
    n = arr.size
    out: List[Dict[str, float]] = []
    for _ in range(int(n_paths)):
        if n:
            sample = arr[rng.integers(0, n, size=n)]
        else:
            sample = arr
        out.append(_path_metrics(equity_path_from_trade_pcts(sample, initial), initial, years))
    return out


def mc_shuffle(pcts, initial: float, n_paths: int, seed: int, years: float = 3.0) -> List[Dict[str, float]]:
    """Order shuffle: per path, a PERMUTATION of the same trades.

    Same trades in a different order -> identical compounded ``final_equity`` (commutative
    product) but a different, path-dependent ``max_drawdown``. Deterministic given ``seed``.
    """
    arr = np.asarray(list(pcts), dtype=float)
    rng = np.random.default_rng(seed)
    out: List[Dict[str, float]] = []
    for _ in range(int(n_paths)):
        perm = rng.permutation(arr)
        out.append(_path_metrics(equity_path_from_trade_pcts(perm, initial), initial, years))
    return out


def mc_jitter(pcts, initial: float, n_paths: int, seed: int, bp_sigma: float, years: float = 3.0) -> List[Dict[str, float]]:
    """Slippage jitter: per path, add gaussian noise (std ``bp_sigma`` BASIS POINTS) to each pct.

    ``bp_sigma`` is in basis points (1 bp = 0.01% = 0.01 in pct-point units). Deterministic given
    ``seed``. Models per-trade execution noise (was the edge inside the spread?).
    """
    arr = np.asarray(list(pcts), dtype=float)
    rng = np.random.default_rng(seed)
    sigma_pct = float(bp_sigma) / 100.0  # bp -> pct points
    out: List[Dict[str, float]] = []
    for _ in range(int(n_paths)):
        noisy = arr + rng.normal(0.0, sigma_pct, size=arr.size) if arr.size else arr
        out.append(_path_metrics(equity_path_from_trade_pcts(noisy, initial), initial, years))
    return out


# ---------------------------------------------------------------------------
# Drop-K best ("was it luck?")
# ---------------------------------------------------------------------------
def drop_k_best(trades: List[Dict[str, Any]], k: int, initial: float, years: float) -> Dict[str, Any]:
    """DETERMINISTICALLY drop the ``k`` highest-``pnl_pct`` trades and metric the rest.

    The proper luck detector: trades are never AVOIDED (that would need a re-run), only the
    ranking view changes — we ask "how much of the return came from the top-K winners?".

    Returns ``{"dropped": [pcts sorted highest-first], **_path_metrics}``. Ties are broken by
    original index so the result is stable.
    """
    pcts = [float(t.get("pnl_pct") or 0.0) for t in trades]
    # Sort indices by pnl_pct descending, stable (ties -> lower original index first).
    order = sorted(range(len(pcts)), key=lambda i: (-pcts[i], i))
    drop_idx = set(order[: int(k)])
    dropped = [pcts[i] for i in order[: int(k)]]
    kept = [pcts[i] for i in range(len(pcts)) if i not in drop_idx]
    metrics = _path_metrics(equity_path_from_trade_pcts(kept, initial), initial, years)
    return {"dropped": dropped, **metrics}


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------
_BAND_KEYS = ("annualized_return", "max_drawdown", "calmar")
_PCTS = (("p5", 5), ("p25", 25), ("p50", 50), ("p75", 75), ("p95", 95))


def summarize_paths(paths: List[Dict[str, Any]], target_annual: float, dd_limit: float) -> Dict[str, Any]:
    """Percentile bands + goal probabilities over a list of per-path metric dicts.

    For each of ``annualized_return`` / ``max_drawdown`` / ``calmar`` returns
    ``{p5, p25, p50, p75, p95}`` via ``np.percentile``.

    * ``prob_target_annual`` — fraction of paths with ``annualized_return >= target_annual``.
    * ``prob_dd_breach`` — fraction with ``max_drawdown <= -dd_limit`` (max_drawdown is NEGATIVE;
      ``dd_limit`` is a POSITIVE pct, e.g. ``20`` -> breach when the path drew down past -20%).
    """
    n = len(paths)
    bands: Dict[str, Dict[str, float]] = {}
    for key in _BAND_KEYS:
        vals = np.asarray([float(p.get(key, 0.0)) for p in paths], dtype=float)
        if vals.size:
            band = {name: float(np.percentile(vals, q)) for name, q in _PCTS}
        else:
            band = {name: 0.0 for name, _ in _PCTS}
        bands[key] = band

    if n:
        ann = np.asarray([float(p.get("annualized_return", 0.0)) for p in paths], dtype=float)
        dd = np.asarray([float(p.get("max_drawdown", 0.0)) for p in paths], dtype=float)
        prob_target = float(np.count_nonzero(ann >= target_annual) / n)
        prob_breach = float(np.count_nonzero(dd <= -abs(dd_limit)) / n)
    else:
        prob_target = 0.0
        prob_breach = 0.0

    return {
        **bands,
        "n_paths": n,
        "prob_target_annual": prob_target,
        "prob_dd_breach": prob_breach,
    }


# ---------------------------------------------------------------------------
# Optional per-path consistency (reuse strategy_fitness, do NOT duplicate)
# ---------------------------------------------------------------------------
def _consistency_helpers():
    """Import the yearly-return + consistency helpers from ``strategy_fitness`` if present.

    Wrapped in try/except so MC works even before/without the fitness ``consistent_annual_return``
    code — the plan makes this a SOFT dependency. Returns ``(consistency_factor, calendar_year_returns)``
    or ``(None, None)``.
    """
    try:
        from app.services.strategy_fitness import (  # type: ignore
            _consistency_factor,
            _calendar_year_returns,
        )
        return _consistency_factor, _calendar_year_returns
    except Exception:  # noqa: BLE001 — optional reuse; MC must not depend on fitness internals
        return None, None


def _path_consistency(path: np.ndarray, exit_dates: Optional[List[Any]]) -> Optional[float]:
    """Consistency score for one path, bucketing its equity by the trades' ``exit_time`` years.

    Builds a pseudo equity_curve of ``{date, equity}`` (initial point + one point per trade at
    that trade's exit date) and feeds it to the reused ``strategy_fitness`` helpers. Returns None
    when the helpers are unavailable or dates are missing (no yearly bucketing possible).
    """
    factor_fn, years_fn = _consistency_helpers()
    if factor_fn is None or years_fn is None or not exit_dates:
        return None
    if len(exit_dates) != path.size - 1:
        return None
    curve = [{"date": None, "equity": float(path[0])}]
    for i, d in enumerate(exit_dates):
        curve.append({"date": d, "equity": float(path[i + 1])})
    # Drop the undated opening point's date issue by giving it the first exit date if available;
    # _calendar_year_returns tolerates unparseable dates (it skips them), so leaving None is safe.
    try:
        return float(factor_fn(years_fn(curve)))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_monte_carlo(trades: List[Dict[str, Any]], initial: float, years: float, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Orchestrate the configured MC methods + drop-K table over a backtest's trade list.

    ``cfg`` keys (no hidden defaults on the load-bearing ones — the caller/API validates):
      * ``methods``   — subset of ``["bootstrap", "shuffle", "jitter"]``.
      * ``n_paths``   — paths per method.
      * ``seed``      — base seed (each method offsets it so methods aren't correlated).
      * ``drop_k``    — list of K values for the drop-K-best table (e.g. ``[1, 2, 3]``).
      * ``jitter_bp`` — basis-point sigma for the jitter method.

    Returns ``{"methods": {name: summary}, "drop_k": [rows], "n_trades": int, "years": float}``
    where each ``summary`` is ``summarize_paths(...)`` and each drop-K row is ``drop_k_best(...)``.
    A per-path ``consistency`` score is added (median across paths) when the fitness reuse and
    trade ``exit_time`` dates allow yearly bucketing.
    """
    pcts = [float(t.get("pnl_pct") or 0.0) for t in trades]
    exit_dates = [t.get("exit_time") for t in trades]
    seed = int(cfg["seed"])
    n_paths = int(cfg["n_paths"])
    target_annual = float(cfg.get("target_annual", 30.0))
    dd_limit = float(cfg.get("dd_limit", 20.0))
    methods = cfg.get("methods") or []

    method_runners = {
        "bootstrap": lambda s: mc_bootstrap(pcts, initial, n_paths, s, years),
        "shuffle": lambda s: mc_shuffle(pcts, initial, n_paths, s, years),
        "jitter": lambda s: mc_jitter(pcts, initial, n_paths, s, float(cfg.get("jitter_bp") or 0.0), years),
    }

    # Yearly-bucketed consistency is only meaningful on the ORIGINAL trade ordering (resampled /
    # shuffled paths don't preserve the exit-date <-> equity mapping). Compute it once here as a
    # baseline "how consistent was the real curve, year over year" score (soft-dep on fitness).
    baseline_consistency = _path_consistency(
        equity_path_from_trade_pcts(pcts, initial), exit_dates
    )

    out_methods: Dict[str, Any] = {}
    for offset, name in enumerate(methods):
        runner = method_runners.get(name)
        if runner is None:
            continue
        paths = runner(seed + offset)
        summary = summarize_paths(paths, target_annual=target_annual, dd_limit=dd_limit)
        if baseline_consistency is not None:
            summary["consistency"] = baseline_consistency
        out_methods[name] = summary

    drop_k_rows = []
    for k in (cfg.get("drop_k") or []):
        drop_k_rows.append({"k": int(k), **drop_k_best(trades, int(k), initial, years)})

    return {
        "methods": out_methods,
        "drop_k": drop_k_rows,
        "n_trades": len(trades),
        "years": float(years),
    }
