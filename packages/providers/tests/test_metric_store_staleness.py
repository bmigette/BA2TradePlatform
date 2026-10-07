"""Staleness guards of the screener metric-store BUILDER (2026-10-06 frozen-row incident).

The builder used to forward-fill each symbol's last bar onto every later scan date with no age
limit, so a stale OHLCV cache (or a delisted ticker) produced rows that never moved. These tests
pin: the 7-day OHLCV limit, the market-cap/float limits, the pre-build freshness check, the frozen
run detector wired into check_frame_quality, the manifest, and the fundamentals END top-up.
"""
import json
import os

import numpy as np
import pandas as pd
import pytest

from ba2_providers.screener import metric_store as ms


def _ohlcv(first: str, last: str, close_start: float = 10.0) -> "pd.DataFrame":
    """Business-day OHLCV with a moving close and volume (a real market never repeats)."""
    idx = pd.bdate_range(first, last)
    n = len(idx)
    rng = np.random.default_rng(abs(hash((first, last))) % 2 ** 32)
    close = close_start + np.cumsum(rng.normal(0, 0.3, n))
    return pd.DataFrame({"Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close,
                         "Volume": rng.integers(1_000_000, 2_000_000, n).astype(float)}, index=idx)


def _universe(monkeypatch, symbols):
    rows = [{"symbol": s, "marketCap": 5e9, "price": 30.0, "volume": 1e6, "sector": "Tech"}
            for s in symbols]
    monkeypatch.setattr(ms, "_fetch_screener_rows", lambda key: rows)


def _build(tmp_path, frames, **kw):
    store = str(tmp_path / "store")
    return store, ms.build_store(
        store, "key", "2026-01-03", "2026-03-28", market_cap_min=0, price_min=0, volume_min=0,
        ohlcv_get=lambda sym, end: frames[sym], cadence_days=7, max_workers=1,
        symbol_retries=0, fail_on_quality=kw.pop("fail_on_quality", False), **kw)


# --- 1. staleness limit on the scan-date reindex ------------------------------------------------

def test_scan_rows_older_than_7_days_are_dropped_not_forward_filled(tmp_path, monkeypatch):
    _universe(monkeypatch, ["LIVE", "DEAD"])
    frames = {"LIVE": _ohlcv("2025-01-02", "2026-03-27"),
              "DEAD": _ohlcv("2025-01-02", "2026-01-30")}           # last bar Fri 2026-01-30
    store, summary = _build(tmp_path, frames, allow_stale_symbols=["DEAD"])
    df = ms.load_store(store)
    dead = sorted(df.loc[df["symbol"] == "DEAD", "date"].astype(str))
    live = sorted(df.loc[df["symbol"] == "LIVE", "date"].astype(str))
    # weekly scans from Sat 2026-01-03: scan 01-31 (1d after the last bar) is kept, 02-07 (8d) is
    # dropped, and so is everything after it
    assert dead[-1] == "2026-01-31"
    assert len(live) == len(pd.date_range("2026-01-03", "2026-03-28", freq="7D"))
    assert summary["rows_dropped_stale"] > 0
    assert summary["stale_symbols_allowed"] == ["DEAD"]


def test_holiday_gaps_stay_inside_the_limit():
    idx = pd.to_datetime(["2026-04-01", "2026-04-02", "2026-04-06"])   # Good Friday 04-03 closed
    df = pd.DataFrame({"Close": [10., 11., 12.], "Volume": [1e6] * 3}, index=idx)
    out = ms.compute_daily_metrics(df)
    grid = pd.DatetimeIndex(["2026-04-04", "2026-04-05"])               # Sat/Sun after 04-02
    got = out.reindex(grid, method="ffill", tolerance=pd.Timedelta(days=ms.STALENESS_MAX_DAYS))
    assert got["close"].notna().all()


def test_market_cap_and_float_have_bounded_ffill():
    idx = pd.bdate_range("2026-01-05", periods=140)
    df = pd.DataFrame({"Close": 10.0, "Volume": 1e6}, index=idx)
    mcap = pd.Series([1e9], index=pd.to_datetime(["2026-01-05"]))
    flt = pd.Series([5e7], index=pd.to_datetime(["2026-01-05"]))
    out = ms.compute_daily_metrics(df, market_cap_series=mcap, float_series=flt)
    at = lambda col, d: out.loc[pd.Timestamp(d), col]                    # noqa: E731
    assert at("market_cap", "2026-01-19") == 1e9                         # 14d old: held
    assert np.isnan(at("market_cap", "2026-01-20"))                      # 15d old: NaN
    assert at("float_shares", "2026-05-05") == 5e7                       # 120d old: held
    assert np.isnan(at("float_shares", "2026-05-06"))                    # 121d old: NaN


# --- 2. pre-build freshness check ---------------------------------------------------------------

def test_prebuild_check_fails_loudly_and_writes_nothing(tmp_path, monkeypatch):
    _universe(monkeypatch, ["LIVE", "STALE1", "STALE2"])
    frames = {"LIVE": _ohlcv("2025-01-02", "2026-03-27"),
              "STALE1": _ohlcv("2025-01-02", "2025-12-31"),
              "STALE2": _ohlcv("2025-01-02", "2026-03-01")}
    store = str(tmp_path / "store")
    with pytest.raises(ms.MetricStoreStaleInputError) as ei:
        ms.build_store(store, "key", "2026-01-03", "2026-03-28", market_cap_min=0, price_min=0,
                       volume_min=0, ohlcv_get=lambda s, e: frames[s], max_workers=1)
    msg = str(ei.value)
    assert "STALE1" in msg and "STALE2" in msg and "LIVE:" not in msg
    assert "--allow-stale-symbols" in msg
    assert not os.path.exists(store) or ms.existing_months(store) == set()


def test_allow_stale_symbols_is_explicit_and_recorded(tmp_path, monkeypatch):
    _universe(monkeypatch, ["LIVE", "GONE", "STALE"])
    frames = {"LIVE": _ohlcv("2025-01-02", "2026-03-27"),
              "GONE": _ohlcv("2025-01-02", "2026-01-15"),
              "STALE": _ohlcv("2025-01-02", "2026-02-10")}
    with pytest.raises(ms.MetricStoreStaleInputError) as ei:   # allowing GONE only is not enough
        _build(tmp_path, frames, allow_stale_symbols=["GONE"])
    assert "STALE" in str(ei.value) and "GONE:" not in str(ei.value)
    store, summary = _build(tmp_path, frames, allow_stale_symbols=["gone", "STALE"])
    assert summary["stale_symbols_allowed"] == ["GONE", "STALE"]
    man = json.load(open(os.path.join(store, "build_manifest.json")))
    assert set(man["builds"][-1]["stale_symbols_allowed"]) == {"GONE", "STALE"}


# --- 3. frozen-run detection --------------------------------------------------------------------

def _scans(vals, vol=1e6, rvol=1.0, sym="X"):
    return pd.DataFrame({"symbol": sym, "date": [f"2026-02-{d:02d}" for d in range(1, 1 + len(vals))],
                         "close": vals, "volume": vol, "relative_volume": rvol})


def test_frozen_runs_flagged_from_three_identical_scans():
    assert ms.find_frozen_runs(_scans([5., 5., 6., 7.])).empty            # two identical: fine
    runs = ms.find_frozen_runs(_scans([5., 5., 5., 7.]))                   # three identical: frozen
    assert len(runs) == 1 and runs.iloc[0]["n_scans"] == 3
    assert ms.check_frame_quality(_scans([5., 5., 5., 7.]), thresholds={}).get("frozen_runs") == 3.0


def test_frozen_runs_exempt_zero_volume_and_isolated_per_symbol():
    assert ms.find_frozen_runs(_scans([5.] * 6, vol=0.0)).empty
    # dormant SPAC: a 20d-average volume that repeats while TODAY's volume is zero (rvol == 0)
    assert ms.find_frozen_runs(_scans([5.] * 6, vol=1830.0, rvol=0.0)).empty
    mixed = pd.concat([_scans([5., 5.], sym="A"), _scans([5., 5.], sym="B")], ignore_index=True)
    assert ms.find_frozen_runs(mixed).empty                                # runs never span symbols


def test_build_fails_on_frozen_rows_even_when_staleness_gate_is_bypassed(tmp_path, monkeypatch):
    """A flat, constant-volume symbol (what a frozen cache looks like) fails the build."""
    _universe(monkeypatch, ["FLAT"])
    idx = pd.bdate_range("2025-01-02", "2026-03-27")
    frames = {"FLAT": pd.DataFrame({"Open": 7., "High": 7., "Low": 7., "Close": 7.,
                                    "Volume": 1e6}, index=idx)}
    with pytest.raises(ms.MetricStoreQualityError) as ei:
        _build(tmp_path, frames, fail_on_quality=True)
    assert "frozen" in str(ei.value).lower()


# --- 4. manifest --------------------------------------------------------------------------------

def test_manifest_records_rule_distribution_and_drops(tmp_path, monkeypatch):
    _universe(monkeypatch, ["LIVE", "GONE"])
    frames = {"LIVE": _ohlcv("2025-01-02", "2026-03-27"),
              "GONE": _ohlcv("2025-01-02", "2026-01-15")}
    store, summary = _build(tmp_path, frames, allow_stale_symbols=["GONE"])
    b = json.load(open(summary["manifest"]))["builds"][-1]
    assert b["staleness_rule"]["ohlcv_row_max_age_days"] == 7
    assert b["staleness_rule"]["market_cap_max_age_days"] == ms.MCAP_MAX_AGE_DAYS
    d = b["ohlcv_last_bar_distribution"]
    assert d["n_symbols"] == 2 and d["days_before_end_buckets"]["<=3d"] == 1
    assert d["days_before_end_buckets"]["31-90d"] == 1 and d["min_last_bar"] == "2026-01-15"
    assert b["dropped_rows"]["rows_dropped_stale"] == summary["rows_dropped_stale"] > 0
    assert sum(b["dropped_rows"]["dropped_by_month"].values()) == summary["rows_dropped_stale"]
    assert b["start"] == "2026-01-03" and b["end"] == "2026-03-28"


# --- 5. fundamentals END top-up -----------------------------------------------------------------

def _fake_http(calls, rows):
    def _get(url, params=None, **kw):
        calls.append(dict(params))

        class _R:
            @staticmethod
            def json():
                return rows
        return _R()
    return _get


def test_market_cap_cache_is_topped_up_past_its_newest_row(tmp_path, monkeypatch):
    path = tmp_path / "AAA.parquet"
    monkeypatch.setattr(ms, "_fund_cache_path", lambda kind, sym: str(path))
    # legacy cache: fetched_from only (no fetched_to), newest row 2026-06-30
    pd.DataFrame({"date": ["2026-06-29", "2026-06-30"], "market_cap": [1e9, 1.1e9]}).to_parquet(path)
    (tmp_path / "AAA.parquet.meta.json").write_text(json.dumps({"fetched_from": "2025-09-01"}))
    calls = []
    monkeypatch.setattr(ms, "fmp_http_get",
                        _fake_http(calls, [{"date": "2026-09-25", "marketCap": 1.5e9}]))
    s = ms.fetch_historical_market_cap("AAA", "key", "2026-01-01", "2026-09-26")
    assert len(calls) == 1 and calls[0]["from"] == "2026-07-01"        # tail only, not the history
    assert s.index.max() == pd.Timestamp("2026-09-25") and len(s) == 3
    ms.fetch_historical_market_cap("AAA", "key", "2026-01-01", "2026-09-26")
    assert len(calls) == 1                                              # now covered: no new call


def test_market_cap_cache_already_covering_end_makes_no_call(tmp_path, monkeypatch):
    path = tmp_path / "AAA.parquet"
    monkeypatch.setattr(ms, "_fund_cache_path", lambda kind, sym: str(path))
    pd.DataFrame({"date": ["2026-06-30"], "market_cap": [1e9]}).to_parquet(path)
    (tmp_path / "AAA.parquet.meta.json").write_text(json.dumps({"fetched_from": "2025-09-01"}))
    monkeypatch.setattr(ms, "fmp_http_get", lambda *a, **k: pytest.fail("network call"))
    s = ms.fetch_historical_market_cap("AAA", "key", "2026-01-01", "2026-07-03")
    assert len(s) == 1


def test_float_cache_refetched_only_when_it_does_not_reach_end(tmp_path, monkeypatch):
    path = tmp_path / "AAA.parquet"
    monkeypatch.setattr(ms, "_fund_cache_path", lambda kind, sym: str(path))
    pd.DataFrame({"date": ["2026-03-01"], "float_shares": [5e7]}).to_parquet(path)
    (tmp_path / "AAA.parquet.meta.json").write_text(json.dumps({"fetched_from": "2025-09-01"}))
    calls = []
    monkeypatch.setattr(ms, "fmp_http_get", _fake_http(
        calls, [{"date": "2026-08-01", "floatShares": 6e7, "filingDate": "2026-08-05"}]))
    ms.fetch_historical_float("AAA", "key", "2026-01-01", "2026-09-26")
    assert len(calls) == 1                                              # cache ended 03-01 < end
    ms.fetch_historical_float("AAA", "key", "2026-01-01", "2026-09-26")
    assert len(calls) == 1                                              # fetched_to now covers it


# --- 6. explicit universe (--symbols-file) -------------------------------------------------------

def _screen_rows(monkeypatch, rows):
    monkeypatch.setattr(ms, "_fetch_screener_rows", lambda key: rows)


def test_universe_symbols_restricts_build_and_ignores_floors(tmp_path, monkeypatch):
    rows = [{"symbol": s, "marketCap": 1.0, "price": 1.0, "volume": 1.0, "sector": "Tech"}
            for s in ("AAA", "BBB", "CCC")]
    _screen_rows(monkeypatch, rows)
    frames = {s: _ohlcv("2025-01-02", "2026-03-27") for s in ("AAA", "BBB", "CCC", "ZZZ")}
    f = tmp_path / "u.txt"
    f.write_text("AAA\nZZZ\n")
    store = str(tmp_path / "store")
    summary = ms.build_store(store, "k", "2026-01-03", "2026-03-28", market_cap_min=1e12,
                             price_min=1e6, volume_min=1e12, ohlcv_get=lambda s, e: frames[s],
                             max_workers=1, universe_symbols=["AAA", "ZZZ"],
                             universe_symbols_path=str(f), fail_on_quality=False)
    df = ms.load_store(store)
    assert set(df["symbol"].astype(str)) == {"AAA", "ZZZ"}          # exactly the list, floors ignored
    assert df.loc[df["symbol"] == "AAA", "sector"].astype(str).iloc[0] == "Tech"
    u = json.load(open(summary["manifest"]))["builds"][-1]["universe"]
    assert u["source"] == "symbols_file" and u["n_symbols"] == 2 and u["path"] == str(f)
    assert u["not_in_screener"] == ["ZZZ"] and u["floors_applied"] is False
    assert len(u["file_sha256"]) == 64 and len(u["symbols_sha256"]) == 64


def test_universe_prebuild_check_iterates_the_exact_set(tmp_path, monkeypatch):
    _screen_rows(monkeypatch, [{"symbol": "AAA"}, {"symbol": "OLD"}, {"symbol": "OTHER"}])
    frames = {"AAA": _ohlcv("2025-01-02", "2026-03-27"), "OLD": _ohlcv("2025-01-02", "2025-06-01"),
              "OTHER": _ohlcv("2025-01-02", "2025-06-01")}
    with pytest.raises(ms.MetricStoreStaleInputError) as ei:
        ms.build_store(str(tmp_path / "s"), "k", "2026-01-03", "2026-03-28", market_cap_min=0,
                       price_min=0, volume_min=0, ohlcv_get=lambda s, e: frames[s], max_workers=1,
                       universe_symbols=["AAA", "OLD"])
    assert "OLD" in str(ei.value) and "OTHER" not in str(ei.value)


def test_default_universe_unchanged_and_manifest_records_thresholds(tmp_path, monkeypatch):
    _universe(monkeypatch, ["LIVE"])
    frames = {"LIVE": _ohlcv("2025-01-02", "2026-03-27")}
    store, summary = _build(tmp_path, frames)
    u = json.load(open(summary["manifest"]))["builds"][-1]["universe"]
    assert u == {"source": "screener_thresholds", "market_cap_min": 0, "price_min": 0,
                 "volume_min": 0, "n_symbols": 1}
