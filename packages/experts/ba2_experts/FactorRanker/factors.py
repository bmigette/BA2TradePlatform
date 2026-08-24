"""Pure factor calculators and combine/rank helpers for FactorRanker.

Every function here is pure (no DB, no network, no broker) so it can be unit
tested directly against known inputs. The expert orchestrates these; data
fetching lives in ``data.py`` and execution in ``portfolio.py``.
"""

from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd


def momentum_12_1(prices: Dict[str, pd.Series], lookback: int = 252, skip: int = 21) -> Dict[str, float]:
    """12-1 month total return: P[-skip] / P[-lookback] - 1.

    Skips the most recent ``skip`` days to avoid short-term reversal. Symbols with
    insufficient history (fewer than ``lookback`` points) score 0.0.
    """
    out: Dict[str, float] = {}
    for sym, s in prices.items():
        s = s.dropna()
        if len(s) < lookback:
            out[sym] = 0.0
            continue
        p_start = float(s.iloc[-lookback])
        p_end = float(s.iloc[-skip - 1])
        out[sym] = (p_end / p_start - 1.0) if p_start > 0 else 0.0
    return out


def earnings_surprise(data: Dict[str, dict], drift_window_days: int = 60) -> Dict[str, float]:
    """Standardized unexpected earnings (SUE), zeroed outside the post-earnings drift window.

    ``data[sym]`` carries ``actual``, ``estimate``, ``estimate_std`` and ``days_since``
    (days since the earnings report). SUE = (actual - estimate) / estimate_std, but only
    while ``days_since`` is within ``drift_window_days`` and the std is positive; otherwise 0.
    """
    out: Dict[str, float] = {}
    for sym, d in data.items():
        days = d.get("days_since")
        std = d.get("estimate_std") or 0.0
        if days is None or days > drift_window_days or std <= 0:
            out[sym] = 0.0
            continue
        out[sym] = (float(d["actual"]) - float(d["estimate"])) / std
    return out


def value_score(data: Dict[str, dict]) -> Dict[str, float]:
    """Composite value: equal-weight of earnings yield (E/P) and FCF/EV yield.

    Higher = cheaper. Missing inputs for a leg contribute 0 to that leg.
    """
    out: Dict[str, float] = {}
    for sym, d in data.items():
        ey = (d["eps_ttm"] / d["price"]) if d.get("eps_ttm") and d.get("price") else 0.0
        fcfy = (d["fcf_ttm"] / d["enterprise_value"]) if d.get("fcf_ttm") and d.get("enterprise_value") else 0.0
        out[sym] = 0.5 * ey + 0.5 * fcfy
    return out


def quality_score(data: Dict[str, dict]) -> Dict[str, float]:
    """Quality = ROE + gross profitability (gross_profit/total_assets) - accruals_ratio.

    Higher = more profitable with cleaner (lower-accrual) earnings. Missing inputs
    contribute 0 to their term.
    """
    out: Dict[str, float] = {}
    for sym, d in data.items():
        roe = d.get("roe") or 0.0
        gp = (d["gross_profit"] / d["total_assets"]) if d.get("gross_profit") and d.get("total_assets") else 0.0
        accr = d.get("accruals_ratio") or 0.0
        out[sym] = roe + gp - accr
    return out


def _winsorized_array(values: Dict[str, float], winsorize_pct: float):
    """The array the z-score is ACTUALLY taken over (post-winsorize), plus its
    symbol order. Shared by cross_sectional_zscore and cross_sectional_stats so
    the reported comparator can never drift from the one used."""
    syms = list(values)
    arr = np.array([values[s] for s in syms], dtype=float)
    if winsorize_pct > 0 and len(arr) > 2:
        lo, hi = np.quantile(arr, [winsorize_pct, 1 - winsorize_pct])
        arr = np.clip(arr, lo, hi)
    return syms, arr


def cross_sectional_zscore(values: Dict[str, float], winsorize_pct: float = 0.0) -> Dict[str, float]:
    """Z-score raw factor values across the universe (mean 0, std 1).

    Optionally winsorize the tails at ``winsorize_pct`` before standardizing.
    If the cross-section has zero dispersion, all z-scores are 0.
    """
    syms, arr = _winsorized_array(values, winsorize_pct)
    mu, sd = arr.mean(), arr.std()
    z = (arr - mu) / sd if sd > 0 else np.zeros_like(arr)
    return {s: float(z[i]) for i, s in enumerate(syms)}


def cross_sectional_stats(values: Dict[str, float], winsorize_pct: float = 0.0) -> Dict[str, Any]:
    """The comparator ``cross_sectional_zscore`` measured against, made visible.

    Returns ``n`` / ``mean`` / ``sd`` of the (post-winsorize) cross-section and
    ``degenerate``: True when ``sd == 0``, i.e. the branch where every z-score
    is forced to exactly 0 REGARDLESS of the underlying values. That happens
    for a one-symbol universe by definition, and for any universe whose members
    all carry the same value -- in both cases 0.0 means "not computable here",
    not "average".
    """
    _, arr = _winsorized_array(values, winsorize_pct)
    if arr.size == 0:
        return {"n": 0, "mean": None, "sd": 0.0, "degenerate": True,
                "winsorize_pct": float(winsorize_pct)}
    sd = float(arr.std())
    return {"n": int(arr.size), "mean": float(arr.mean()), "sd": sd,
            "degenerate": not (sd > 0), "winsorize_pct": float(winsorize_pct)}


def describe_composite_availability(universe_size: int,
                                    factor_stats: Dict[str, Dict[str, Any]],
                                    weights: Dict[str, float]) -> Tuple[bool, Optional[str]]:
    """Is the composite a real measurement here, and if not, why not?

    The composite is a weighted sum of cross-sectional z-scores. When every
    CONTRIBUTING factor's cross-section is degenerate the sum is arithmetically
    pinned to 0.0 and carries no information about the symbol -- reporting that
    as a number invites it to be read as "neutral", which is a different and
    false statement.

    ``factor_stats`` may be empty (a book stored before these stats existed);
    the universe size alone then decides, which is the only structurally
    certain case.
    """
    if universe_size <= 1:
        return False, (
            f"Not computable in this view. The composite is a weighted sum of "
            f"CROSS-SECTIONAL z-scores, which need a peer universe to rank against; "
            f"this card scores exactly 1 symbol, so the standard deviation of the "
            f"cross-section is 0 and every z-score — and therefore the composite — is "
            f"forced to +0.000 whatever the underlying factor values are. "
            f"See the raw per-factor values below for the numbers that were actually "
            f"measured.")
    contributing = [n for n, w in (weights or {}).items() if w] or list(factor_stats or {})
    stats = [factor_stats[n] for n in contributing if n in (factor_stats or {})]
    if stats and all(s.get("degenerate") for s in stats):
        names = ", ".join(n for n in contributing if n in factor_stats)
        return False, (
            f"Not computable in this view. Every weighted factor ({names}) has ZERO "
            f"dispersion across the {universe_size}-symbol universe, so its "
            f"cross-sectional z-score is forced to 0 for every name and the composite "
            f"with it. See the raw per-factor values below.")
    return True, None


def composite_score(factor_values: Dict[str, Dict[str, float]], weights: Dict[str, float],
                    winsorize_pct: float = 0.0) -> Dict[str, float]:
    """Weighted sum of per-factor cross-sectional z-scores.

    ``factor_values`` maps factor name -> {symbol: raw value}. Each factor is
    z-scored across the universe, then multiplied by its weight and summed.
    A weight of 0 disables that factor.
    """
    # Sort the unioned symbols so the composite dict has a deterministic key order.
    # A raw ``set`` union iterates in a process-dependent order (PYTHONHASHSEED);
    # that order becomes the insertion order of ``out`` and would then drive
    # ``rank_symbols``' stable-sort tie-break, making equal-score symbols rank in a
    # non-deterministic order across runs. Sorting here pins the key order.
    symbols = sorted(set().union(*[set(v) for v in factor_values.values()])) if factor_values else []
    out = {s: 0.0 for s in symbols}
    for fname, vals in factor_values.items():
        w = weights.get(fname, 0.0)
        if w == 0.0:
            continue
        z = cross_sectional_zscore(vals, winsorize_pct)
        for s in symbols:
            out[s] += w * z.get(s, 0.0)
    return out


def rank_symbols(composite: Dict[str, float]) -> List[str]:
    """Symbols sorted by composite score, highest first.

    Ties are broken DETERMINISTICALLY by symbol (ascending). Without an explicit
    tie-break, ``sorted`` (being stable) preserves the input dict's key order for
    equal scores; that order can be process-dependent, so equal-score names would
    flip places run-to-run and change the top-N cut. Sorting by ``(-score, symbol)``
    makes the ranking — and therefore the held book — bit-stable.
    """
    return sorted(composite, key=lambda s: (-composite[s], s))
