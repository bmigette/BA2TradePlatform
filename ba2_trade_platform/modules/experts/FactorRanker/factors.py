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
