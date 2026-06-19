import os
from unittest.mock import patch

import numpy as np
import pandas as pd

from ba2_providers.screener import metric_store as ms

_FAKE_SCREEN = [
    {"symbol": "AAA", "marketCap": 5e9, "price": 30.0, "volume": 2_000_000, "sector": "Tech"},
    {"symbol": "BBB", "marketCap": 8e8, "price": 12.0, "volume": 100_000, "sector": "Energy"},  # below cap
    {"symbol": "CCC", "marketCap": 3e9, "price": 40.0, "volume": 900_000, "sector": "Tech"},
]


def test_enumerate_universe_applies_loosest_static_prefilter():
    with patch.object(ms, "_fetch_screener_rows", return_value=_FAKE_SCREEN):
        # loosest bounds: cap>=1e9, price>=10, vol>=500k -> AAA, CCC (BBB drops on cap+vol)
        rows = ms.enumerate_universe(api_key="x", market_cap_min=1e9, price_min=10.0, volume_min=500_000)
    assert {r["symbol"] for r in rows} == {"AAA", "CCC"}
    assert rows[0]["sector"]  # static fields preserved


def _ohlcv(n=40, start_close=100.0):
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    close = pd.Series(np.linspace(start_close, start_close + n, n), index=idx)
    vol = pd.Series([1_000_000] * n, index=idx, dtype=float)
    vol.iloc[-1] = 3_000_000  # last day spikes -> RVOL ~3x
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 5, "Close": close, "Volume": vol})


def test_daily_metrics_rvol_and_dip_are_vectorised():
    df = _ohlcv()
    out = ms.compute_daily_metrics(df, shares=1_000_000, rvol_window=20, drop_days=5)
    last = out.iloc[-1]
    assert round(last["relative_volume"], 1) == 3.0          # 3M / 1M avg
    assert last["market_cap"] == 1_000_000 * df["Close"].iloc[-1]
    # price-drop %: close is at a rising series' peak -> ~0 drop on the last bar
    assert last["price_drop_pct"] >= 0.0
    assert out.shape[0] == df.shape[0]                        # one row per day (a time series)


def _stage2_closes(n=200, start=50.0, slope=0.5):
    """Steadily RISING closes -> price above a rising 150-SMA -> Weinstein Stage 2 on the tail."""
    idx = pd.date_range("2022-01-03", periods=n, freq="B")
    return pd.Series(np.arange(n, dtype=float) * slope + start, index=idx)


def _stage4_closes(n=200, start=250.0, slope=0.5):
    """Steadily FALLING closes -> price below a falling 150-SMA -> NOT Stage 2 (Stage 4)."""
    idx = pd.date_range("2022-01-03", periods=n, freq="B")
    return pd.Series(start - np.arange(n, dtype=float) * slope, index=idx)


def test_weinstein_stage_vectorised_matches_classifier():
    from ba2_common.core.weinstein import classify_weinstein_stage

    up = _stage2_closes()
    down = _stage4_closes()

    # Vectorised stage on the last bar.
    up_last = ms.weinstein_stage_series(up).iloc[-1]
    down_last = ms.weinstein_stage_series(down).iloc[-1]
    assert up_last == 2.0          # rising series above a rising SMA -> Stage 2
    assert down_last != 2.0        # declining series -> not Stage 2

    # Cross-check against the canonical classifier on the SAME series (the agreement that lets
    # the fast path replace the slow StockScreener Stage-2 filter).
    assert classify_weinstein_stage(list(up)).get("stage") == 2
    assert classify_weinstein_stage(list(down)).get("stage") != 2
    assert int(up_last) == classify_weinstein_stage(list(up))["stage"]
    assert int(down_last) == classify_weinstein_stage(list(down))["stage"]

    # Full-series agreement on the stage-2 / not-stage-2 distinction at every bar that has
    # enough history for the classifier (>= 170 bars).
    series = ms.weinstein_stage_series(up)
    closes = list(up)
    for i in range(len(closes)):
        ref = classify_weinstein_stage(closes[: i + 1]).get("stage")
        vec = series.iloc[i]
        if ref is None:
            assert pd.isna(vec)                    # insufficient history -> NaN both ways
        else:
            assert (vec == 2.0) == (ref == 2)      # stage-2 boolean agrees bar-by-bar


def test_compute_daily_metrics_exposes_weinstein_stage():
    df_up = pd.DataFrame({
        "Open": _stage2_closes(), "High": _stage2_closes() + 1, "Low": _stage2_closes() - 1,
        "Close": _stage2_closes(),
        "Volume": pd.Series(1_000_000.0, index=_stage2_closes().index)})
    out = ms.compute_daily_metrics(df_up, shares=1_000_000, rvol_window=20, drop_days=1)
    assert "weinstein_stage" in out.columns
    assert out["weinstein_stage"].iloc[-1] == 2.0          # last bar is Stage 2
    assert pd.isna(out["weinstein_stage"].iloc[0])         # no history early -> NaN (dropped)

    df_down = df_up.assign(Close=_stage4_closes(), Open=_stage4_closes())
    out_down = ms.compute_daily_metrics(df_down, shares=1_000_000)
    assert out_down["weinstein_stage"].iloc[-1] != 2.0     # declining -> not Stage 2


def test_screen_universe_for_day_honors_weinstein_stage2_only():
    df = pd.DataFrame({
        "symbol": ["AAA", "BBB", "CCC"], "date": ["2023-01-31"] * 3,
        "close": [10, 20, 30.0], "market_cap": [5e9, 2e9, 9e9],
        "relative_volume": [2.0, 2.0, 2.0], "price_drop_pct": [0.0, 0.0, 0.0],
        "sector": ["Tech"] * 3, "volume": [2e6] * 3, "price": [10, 20, 30.0],
        "weinstein_stage": [2.0, 4.0, float("nan")]})     # only AAA is Stage 2
    sel = ms.screen_universe_for_day(df, "2023-01-31", {
        "weinstein_stage2_only": 1, "max_stocks": 5, "sort_metric": "market_cap"})
    assert sel == ["AAA"]                                   # BBB (stage 4) + CCC (NaN) dropped
    # 0 / absent -> no-op: all three survive (additive; existing behaviour unchanged).
    assert set(ms.screen_universe_for_day(df, "2023-01-31", {"weinstein_stage2_only": 0})) == \
        {"AAA", "BBB", "CCC"}
    assert set(ms.screen_universe_for_day(df, "2023-01-31", {})) == {"AAA", "BBB", "CCC"}


def test_store_write_is_incremental_by_partition(tmp_path):
    store = str(tmp_path / "mstore")
    df_jan = pd.DataFrame({"symbol": ["AAA"], "date": ["2023-01-31"], "close": [10.0],
                           "market_cap": [1e9], "relative_volume": [1.2], "price_drop_pct": [0.0],
                           "sector": ["Tech"], "volume": [1e6], "price": [10.0]})
    ms.write_partitions(store, df_jan)
    assert os.path.isdir(os.path.join(store, "ym=2023-01"))
    # existing partition is reported as present so build can skip it
    assert ms.existing_months(store) == {"2023-01"}
    # adding Feb does not touch Jan
    df_feb = df_jan.assign(date=["2023-02-28"])
    ms.write_partitions(store, df_feb)
    assert ms.existing_months(store) == {"2023-01", "2023-02"}


def test_load_store_returns_indexed_frame(tmp_path):
    store = str(tmp_path / "s")
    ms.write_partitions(store, pd.DataFrame({
        "symbol": ["AAA", "BBB"], "date": ["2023-01-31", "2023-01-31"], "close": [10.0, 20.0],
        "market_cap": [2e9, 6e8], "relative_volume": [1.5, 0.9], "price_drop_pct": [12.0, 1.0],
        "sector": ["Tech", "Energy"], "volume": [2e6, 1e5], "price": [10.0, 20.0]}))
    df = ms.load_store(store)
    assert set(df["symbol"]) == {"AAA", "BBB"}
    assert "2023-01-31" in set(df["date"])


def test_screen_universe_for_day_applies_thresholds_sort_and_cap():
    df = pd.DataFrame({
        "symbol": ["AAA", "BBB", "CCC"], "date": ["2023-01-31"] * 3,
        "close": [10, 20, 30.0], "market_cap": [5e9, 2e9, 9e8],
        "relative_volume": [2.0, 1.1, 5.0], "price_drop_pct": [20.0, 1.0, 18.0],
        "sector": ["Tech"] * 3, "volume": [2e6, 2e6, 2e6], "price": [10, 20, 30.0]})
    sel = ms.screen_universe_for_day(df, "2023-01-31", {
        "market_cap_min": 1e9,            # drops CCC (9e8)
        "relative_volume_min": 1.5,       # drops BBB (1.1)
        "price_drop_pct": 15.0,           # AAA dip 20 >= 15 ok
        "max_stocks": 5, "sort_metric": "market_cap"})
    assert sel == ["AAA"]                  # only AAA passes all
    # forward day with no rows -> empty
    assert ms.screen_universe_for_day(df, "2023-02-01", {"market_cap_min": 0}) == []


def test_screen_universe_as_of_holds_between_scans():
    df = pd.DataFrame({"symbol": ["AAA"], "date": ["2023-03-06"], "close": [10.0],
        "market_cap": [5e9], "relative_volume": [2.0], "price_drop_pct": [20.0],
        "sector": ["T"], "volume": [2e6], "price": [10.0]})
    # a Thursday with no scan row resolves to the Monday scan
    assert ms.screen_universe_as_of(df, "2023-03-09", {"market_cap_min": 1e9}) == ["AAA"]
    assert ms.screen_universe_as_of(df, "2023-03-01", {"market_cap_min": 1e9}) == []  # before first scan
