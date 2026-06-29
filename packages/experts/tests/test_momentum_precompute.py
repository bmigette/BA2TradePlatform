"""The precomputed metric-store momentum column must equal the runtime FactorRanker factor.

FactorRanker reads ``momentum_12_1`` point-in-time from the metric store (built by
``ba2_providers.screener.metric_store.momentum_12_1_series``) instead of fetching ~400 days of
daily closes per symbol per rebalance. This test pins those two to be byte-identical so the
optimization is results-preserving.
"""
import numpy as np
import pandas as pd

from ba2_providers.screener.metric_store import momentum_12_1_series
from ba2_experts.FactorRanker.factors import momentum_12_1


def test_precomputed_momentum_matches_runtime_factor():
    rng = np.random.RandomState(7)
    n = 600
    # A realistic positive random-walk close series.
    close = pd.Series(100.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.02, n)))

    pre = momentum_12_1_series(close)  # per-bar precomputed series

    # For several point-in-time bars D, the runtime factor on closes[:D+1] must equal pre[D].
    for D in (251, 252, 300, 450, n - 1):
        runtime = momentum_12_1({"X": close.iloc[: D + 1]})["X"]  # the factor's 0.0-on-short rule
        precomp = pre.iloc[D]
        precomp = 0.0 if pd.isna(precomp) else float(precomp)
        assert abs(precomp - runtime) < 1e-9, f"bar {D}: pre={precomp} runtime={runtime}"

    # Insufficient history (< lookback) -> factor returns 0.0; precompute is NaN (consumer maps ->0).
    short = close.iloc[:100]
    assert momentum_12_1({"X": short})["X"] == 0.0
    assert pd.isna(momentum_12_1_series(short).iloc[-1])


def test_precomputed_momentum_zero_on_nonpositive_start():
    close = pd.Series([0.0] * 260 + [10.0] * 10)  # start price 0 -> factor returns 0.0
    assert momentum_12_1({"X": close})["X"] == 0.0
    # precompute -> NaN where start <= 0 (consumer maps to 0.0)
    assert pd.isna(momentum_12_1_series(close).iloc[-1])
