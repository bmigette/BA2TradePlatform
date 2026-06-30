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


def test_compute_daily_metrics_momentum_and_metrics_as_of_match_factor():
    """End-to-end build path: compute_daily_metrics' momentum_12_1 column AND a daily-store
    metrics_as_of read both equal the runtime factor at the exact bar (byte-identical daily ranking).
    """
    from ba2_providers.screener.metric_store import compute_daily_metrics, metrics_as_of

    rng = np.random.RandomState(3)
    n = 400
    dates = pd.bdate_range("2022-01-03", periods=n)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.02, n))
    ohlcv = pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1e6}, index=dates
    )
    m = compute_daily_metrics(ohlcv)
    assert "momentum_12_1" in m.columns

    D = n - 1
    runtime = momentum_12_1({"X": pd.Series(close[: D + 1])})["X"]
    col = m["momentum_12_1"].iloc[D]
    col = 0.0 if pd.isna(col) else float(col)
    assert abs(col - runtime) < 1e-6  # column is round(6)

    # A DAILY store: metrics_as_of lands on the exact bar -> same value the consumer reads.
    store_df = m.reset_index().rename(columns={"index": "date"})
    store_df["date"] = pd.to_datetime(store_df["date"]).dt.strftime("%Y-%m-%d")
    store_df["symbol"] = "X"
    got = metrics_as_of(store_df, store_df["date"].iloc[D], ["momentum_12_1", "close"])
    assert "X" in got
    read = got["X"]["momentum_12_1"]
    read = 0.0 if pd.isna(read) else float(read)
    assert abs(read - runtime) < 1e-6
    assert abs(float(got["X"]["close"]) - float(close[D])) < 1e-6  # value-factor price source


def test_precomputed_momentum_zero_on_nonpositive_start():
    close = pd.Series([0.0] * 260 + [10.0] * 10)  # start price 0 -> factor returns 0.0
    assert momentum_12_1({"X": close})["X"] == 0.0
    # precompute -> NaN where start <= 0 (consumer maps to 0.0)
    assert pd.isna(momentum_12_1_series(close).iloc[-1])
