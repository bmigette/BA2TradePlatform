"""Pure factor calculators and combine/rank helpers for FactorRanker.

Every function here is pure (no DB, no network, no broker) so it can be unit
tested directly against known inputs. The expert orchestrates these; data
fetching lives in ``data.py`` and execution in ``portfolio.py``.
"""

from typing import Dict
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
