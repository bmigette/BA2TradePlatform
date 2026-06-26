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


# --- point-in-time fundamentals (volume / market_cap / float) -----------------

def test_volume_is_trailing_average_point_in_time():
    """volume = trailing-avg daily volume ENDING at each bar (point-in-time), NOT a single static
    value, and never depends on future bars (no lookahead)."""
    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    df = pd.DataFrame({"Close": [10.0] * 6,
                       "Volume": [100, 200, 300, 400, 500, 600.0]}, index=idx)
    out = ms.compute_daily_metrics(df, vol_window=3, rvol_window=3)
    # bar 3 (0-idx 2): mean(100,200,300)=200; bar 5: mean(400,500,600)=500 -> trailing, not static
    assert out["volume"].iloc[2] == 200.0
    assert out["volume"].iloc[4] == 400.0
    # no-lookahead: truncating the future leaves earlier bars' volume unchanged
    out_trunc = ms.compute_daily_metrics(df.iloc[:3], vol_window=3, rvol_window=3)
    assert out["volume"].iloc[2] == out_trunc["volume"].iloc[2]


def test_market_cap_from_historical_series_as_of():
    """market_cap comes from the historical series sampled AS-OF (ffill), overriding the legacy
    close x static-shares path; updates only on/after each series date (no lookahead)."""
    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    df = pd.DataFrame({"Close": [10.0] * 6, "Volume": [1e6] * 6}, index=idx)
    mcap = pd.Series([1e9, 2e9], index=pd.to_datetime(["2024-01-01", "2024-01-04"]))
    out = ms.compute_daily_metrics(df, market_cap_series=mcap, shares=999)  # series wins over shares
    assert out["market_cap"].iloc[2] == 1e9          # 01-03: pre-update value held
    assert out["market_cap"].iloc[3] == 2e9          # 01-04: as-of update
    assert out["market_cap"].iloc[0] != 10.0 * 999   # NOT close x static shares


def test_float_series_as_of_and_min_max_filter():
    """float_shares is sampled as-of from the historical series and gated by float_min/float_max."""
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    df = pd.DataFrame({"Close": [10.0] * 5, "Volume": [1e6] * 5}, index=idx)
    flt = pd.Series([5e7, 8e7], index=pd.to_datetime(["2024-01-01", "2024-01-03"]))
    out = ms.compute_daily_metrics(df, float_series=flt)
    assert out["float_shares"].iloc[1] == 5e7        # 01-02: pre-update held
    assert out["float_shares"].iloc[2] == 8e7        # 01-03: as-of update
    store = pd.DataFrame({"symbol": ["LOW", "HIGH"], "date": ["2024-01-05"] * 2,
                          "close": [10, 20.0], "market_cap": [1e9, 2e9], "price": [10, 20.0],
                          "relative_volume": [1.0, 1.0], "price_drop_pct": [0.0, 0.0],
                          "volume": [1e6, 1e6], "float_shares": [2e7, 8e7]})
    assert ms.screen_universe_for_day(store, "2024-01-05", {"float_min": 5e7}) == ["HIGH"]
    assert ms.screen_universe_for_day(store, "2024-01-05", {"float_max": 5e7}) == ["LOW"]


def test_float_gate_is_nan_tolerant_for_mixed_schema_store():
    """An incrementally-rebuilt MIXED-schema store has NaN float on legacy-month rows (no
    float_shares before the column existed). The float gate must NOT silently drop those rows
    (graceful degradation): unknown float passes."""
    store = pd.DataFrame({
        "symbol": ["OLD", "NEW", "LOWF"], "date": ["2024-01-08"] * 3,
        "close": [10, 20, 5.0], "market_cap": [2e9, 3e9, 1e9], "price": [10, 20, 5.0],
        "relative_volume": [1.0, 1.0, 1.0], "price_drop_pct": [0.0, 0.0, 0.0],
        "volume": [1e6, 2e6, 5e5], "float_shares": [float("nan"), 8e7, 2e7]})
    sel = ms.screen_universe_for_day(store, "2024-01-08", {"float_min": 5e7})
    assert set(sel) == {"OLD", "NEW"}                # OLD (NaN) kept; NEW passes; LOWF excluded


def test_screen_legacy_store_without_float_column_skips_float_gate():
    """A pure legacy store (no float_shares column at all) screens with the float gate as a no-op."""
    store = pd.DataFrame({"symbol": ["AAA"], "date": ["2024-01-08"], "close": [10.0],
                          "market_cap": [2e9], "price": [10.0], "relative_volume": [1.0],
                          "price_drop_pct": [0.0], "volume": [1e6]})
    assert ms.screen_universe_for_day(store, "2024-01-08", {"float_min": 1e9}) == ["AAA"]


def test_float_series_built_lookahead_safe_effective_dated(monkeypatch):
    """fetch_historical_float indexes by the EFFECTIVE (publication) date — period-end + reporting
    lag — so a float is never exposed before it was public (no filing-lag lookahead)."""
    import ba2_providers.screener.metric_store as _ms

    class _Resp:
        @staticmethod
        def json():
            return [{"date": "2024-01-01", "floatShares": 5e7}]  # period-end only (no filing date)
    monkeypatch.setattr(_ms, "fmp_http_get", lambda *a, **k: _Resp())
    monkeypatch.setattr(_ms, "_fund_cache_path", lambda kind, sym: "/nonexistent/no.parquet")
    s = _ms.fetch_historical_float("AAA", "key", "2023-01-01", "2024-12-31")
    assert len(s) == 1
    # the period-end 2024-01-01 must surface only ~75 days later (the reporting lag), not on 01-01
    assert s.index[0] > pd.Timestamp("2024-02-01")


# --- optimizable price-drop lookback (multi-window) ---------------------------

def test_compute_daily_metrics_emits_per_window_drop_columns():
    """compute_daily_metrics writes a per-window price_drop_pct_<W> for W=2..max_lookback (plus the
    legacy single-window price_drop_pct), so the lookback Y is optimizable from ONE store. A bigger
    window can only see an equal-or-higher trailing peak, so its drop is monotonically >= a smaller
    window's at the same bar."""
    idx = pd.date_range("2024-01-01", periods=40, freq="B")
    close = pd.Series(np.concatenate([np.linspace(100, 130, 20), np.linspace(130, 110, 20)]), index=idx)
    df = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close,
                       "Volume": [1e6] * 40})
    out = ms.compute_daily_metrics(df, max_lookback=30)
    wcols = [c for c in out.columns if c.startswith("price_drop_pct_")]
    assert len(wcols) == 29                                   # 2..30 inclusive
    assert "price_drop_pct" in out.columns                    # legacy column retained
    last = out.iloc[-1]
    assert last["price_drop_pct_30"] >= last["price_drop_pct_2"] >= 0.0   # wider window >= drop
    assert last["price_drop_pct_30"] > 0.0                                # the late dip is captured
    # legacy column equals its drop_days window (default 5)
    assert round(float(last["price_drop_pct"]), 4) == round(float(last["price_drop_pct_5"]), 4)


def test_screen_universe_for_day_selects_window_column_from_price_drop_days():
    """price_drop_days (Y) picks the price_drop_pct_<Y> column; the price_drop_pct setting is the
    threshold. AAA only dips on the wide (20d) window, BBB on both — so Y=5 selects BBB, Y=20 both."""
    df = pd.DataFrame({
        "symbol": ["AAA", "BBB"], "date": ["2024-01-31"] * 2,
        "close": [10, 20.0], "market_cap": [5e9, 5e9], "relative_volume": [2.0, 2.0],
        "sector": ["Tech"] * 2, "volume": [2e6] * 2, "price": [10, 20.0],
        "price_drop_pct": [1.0, 12.0],          # legacy (== 5d) column
        "price_drop_pct_5": [1.0, 12.0],
        "price_drop_pct_20": [18.0, 12.0]})
    # Y=5, threshold 10 -> only BBB (AAA 5d-drop is 1)
    assert ms.screen_universe_for_day(df, "2024-01-31", {"price_drop_days": 5, "price_drop_pct": 10.0}) == ["BBB"]
    # Y=20, threshold 10 -> both (AAA 20d-drop is 18)
    assert set(ms.screen_universe_for_day(df, "2024-01-31", {"price_drop_days": 20, "price_drop_pct": 10.0})) == {"AAA", "BBB"}
    # fractional Y (a float gene) truncates to the int column
    assert ms.screen_universe_for_day(df, "2024-01-31", {"price_drop_days": 5.7, "price_drop_pct": 10.0}) == ["BBB"]


def test_screen_universe_for_day_falls_back_to_legacy_when_window_absent():
    """A legacy store (only price_drop_pct, no per-window columns) still screens on the threshold —
    price_drop_days is ignored when its column is missing (back-compat)."""
    df = pd.DataFrame({
        "symbol": ["AAA", "BBB"], "date": ["2024-01-31"] * 2,
        "close": [10, 20.0], "market_cap": [5e9, 5e9], "relative_volume": [2.0, 2.0],
        "sector": ["Tech"] * 2, "volume": [2e6] * 2, "price": [10, 20.0],
        "price_drop_pct": [3.0, 12.0]})
    assert ms.screen_universe_for_day(df, "2024-01-31", {"price_drop_days": 20, "price_drop_pct": 10.0}) == ["BBB"]


def test_recompute_price_drop_columns_cache_only_inplace(tmp_path):
    """recompute_price_drop_columns rebuilds ONLY the drop columns from a (cache-only) daily OHLCV
    getter, in place, leaving every other column untouched and skipping uncached symbols."""
    store = str(tmp_path / "mstore")
    # Two weekly scan rows for AAA (price_drop_pct seeded WRONG = 0, the bug) + BBB (no OHLCV cache).
    seed = pd.DataFrame({
        "symbol": ["AAA", "AAA", "BBB"],
        "date": ["2024-01-31", "2024-02-29", "2024-01-31"],
        "close": [120.0, 110.0, 50.0], "market_cap": [5e9, 5e9, 1e9],
        "relative_volume": [2.0, 2.0, 1.0], "price_drop_pct": [0.0, 0.0, 0.0],
        "sector": ["Tech", "Tech", "Energy"], "volume": [2e6, 2e6, 1e6], "price": [120.0, 110.0, 50.0]})
    ms.write_partitions(store, seed)

    # Daily OHLCV only for AAA: rises to 130 then dips to 110 by end of Feb.
    aaa_idx = pd.date_range("2024-01-01", "2024-02-29", freq="B")
    aaa_close = pd.Series(np.concatenate([
        np.linspace(100, 130, len(aaa_idx) // 2),
        np.linspace(130, 110, len(aaa_idx) - len(aaa_idx) // 2)]), index=aaa_idx)
    aaa = pd.DataFrame({"Open": aaa_close, "High": aaa_close, "Low": aaa_close,
                        "Close": aaa_close, "Volume": [1e6] * len(aaa_idx)})

    def _getter(sym):
        return aaa if sym == "AAA" else None      # BBB has no cache -> skipped

    res = ms.recompute_price_drop_columns(store, _getter, max_lookback=30, drop_days=5)
    assert res["recomputed"] == 1 and res["skipped"] == 1 and res["skipped_symbols"] == ["BBB"]

    ms.clear_store_memo()
    out = ms.load_store(store)
    # windowed columns exist; AAA's Feb dip is now non-zero; other columns intact.
    assert "price_drop_pct_30" in out.columns and "price_drop_pct_5" in out.columns
    aaa_feb = out[(out["symbol"] == "AAA") & (out["date"] == "2024-02-29")].iloc[0]
    assert aaa_feb["price_drop_pct_30"] > 0.0                 # was 0 (the bug), now real
    assert aaa_feb["market_cap"] == 5e9                       # untouched
    # BBB skipped -> its legacy drop stays as seeded, windowed columns are NaN.
    bbb = out[out["symbol"] == "BBB"].iloc[0]
    assert bbb["price_drop_pct"] == 0.0
    assert pd.isna(bbb["price_drop_pct_30"])
    # consolidation: exactly one part.parquet per month (stale flush files removed).
    import glob
    for m in ("2024-01", "2024-02"):
        parts = glob.glob(os.path.join(store, f"ym={m}", "*.parquet"))
        assert parts == [os.path.join(store, f"ym={m}", "part.parquet")]
