"""Panel / metric store / screener provider record and respect their OHLCV history source (Task 8)."""
import json
import os

import pandas as pd
import pytest

from ba2_common.core.ohlcv_history import OHLCV_HISTORY_SOURCE_KEY
from ba2_common.core.ohlcv_history_stamp import HistorySourceMismatch
from ba2_providers.screener import live_sim as ls
from ba2_providers.screener import live_sim_build as lb
from ba2_providers.screener import metric_store as ms

SESSIONS = [d.date() for d in pd.bdate_range("2023-10-02", "2024-03-28")]


def _bars(path, close=100.0):
    df = pd.DataFrame({"Date": pd.to_datetime(SESSIONS), "Open": close, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Volume": 1_000_000})
    df.to_parquet(path, index=False)


def _cache(tmp_path, folders=("FMPOHLCVProvider", "AlpacaHistoryOHLCVProvider")):
    c = tmp_path / "cache"
    fund = c / "screener_fundamentals"
    for k in ("market_cap", "shares", "splits"):
        (fund / k).mkdir(parents=True)
    for f in folders:
        (c / f).mkdir(parents=True)
        _bars(c / f / "A_1d.parquet")
    pd.DataFrame({"date": ["2023-06-01"], "outstanding": [2e7]}).to_parquet(fund / "shares" / "A.parquet", index=False)
    pd.DataFrame({"date": [d.isoformat() for d in SESSIONS], "market_cap": [2e9] * len(SESSIONS)}).to_parquet(
        fund / "market_cap" / "A.parquet", index=False)
    (fund / "splits" / "A.json").write_text(json.dumps({"symbol": "A", "historical": []}))
    snap = c / "screener" / "vendor_shares"
    snap.mkdir(parents=True)
    (snap / "2024-04-01.json").write_text(json.dumps({
        "fetched_at_ny": "2024-04-01T20:00:00", "rows": {"A": 2e7}, "caps": {"A": [2e9, 100.0]}}))
    return c


def _panel(c, source):
    return lb.build_daily_panel(str(c), ["A"], "2023-10-02", "2024-03-28", workers=1, log=lambda m: None,
                                history_source=source)


def test_panel_manifest_records_its_source_and_the_fingerprints_differ(tmp_path):
    c = _cache(tmp_path)
    fmp, alp = _panel(c, "fmp"), _panel(c, "alpaca")
    assert fmp[OHLCV_HISTORY_SOURCE_KEY] == "fmp" and alp[OHLCV_HISTORY_SOURCE_KEY] == "alpaca"
    assert ls.read_manifest(alp["path"])[OHLCV_HISTORY_SOURCE_KEY] == "alpaca"
    assert fmp["panel_fingerprint"] != alp["panel_fingerprint"] and fmp["path"] != alp["path"]
    assert os.path.isdir(fmp["path"]) and os.path.isdir(alp["path"])      # the alpaca build overwrote nothing


def test_panel_reads_the_bars_of_its_own_history(tmp_path):
    c = _cache(tmp_path, folders=("FMPOHLCVProvider",))                  # only the FMP history exists
    assert lb.symbols_without_daily_bars(str(c), ["A"], "fmp") == []
    assert lb.symbols_without_daily_bars(str(c), ["A"], "alpaca") == ["A"]
    man = _panel(c, "alpaca")
    assert man["n_without_daily_bars"] == 1                              # not silently served from the FMP folder


def test_source_fingerprint_depends_on_the_source(tmp_path):
    c = _cache(tmp_path)
    snap = lb.latest_vendor_snapshot(str(c))
    args = (str(c), ["A"], "2023-10-02", "2024-03-28", 1, snap)
    assert lb.source_fingerprint(*args, history_source="fmp") == lb.source_fingerprint(*args, history_source="fmp")
    assert lb.source_fingerprint(*args, history_source="fmp") != lb.source_fingerprint(*args, history_source="alpaca")


def _frames():
    idx = pd.bdate_range("2025-01-02", "2026-03-27")
    k = pd.Series(range(len(idx)), index=idx, dtype=float)
    close = k * 0.01 + 20
    return pd.DataFrame({"Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close,
                         "Volume": 1_500_000.0 + k * 7}, index=idx)


def _build_store(tmp_path, monkeypatch, source, store=None):
    monkeypatch.setattr(ms, "_fetch_screener_rows",
                        lambda key: [{"symbol": "A", "marketCap": 5e9, "price": 30.0, "volume": 1e6, "sector": "T"}])
    store = store or str(tmp_path / "store")
    ms.build_store(store, "k", "2026-01-03", "2026-03-28", market_cap_min=0, price_min=0, volume_min=0,
                   ohlcv_get=lambda s, e: _frames(), ohlcv_history_source=source, max_workers=1,
                   symbol_retries=0, fail_on_quality=False)
    return store


def test_metric_store_manifest_records_the_source(tmp_path, monkeypatch):
    store = _build_store(tmp_path, monkeypatch, "alpaca")
    with open(os.path.join(store, "build_manifest.json")) as f:
        doc = json.load(f)
    assert [b[OHLCV_HISTORY_SOURCE_KEY] for b in doc["builds"]] == ["alpaca"]
    assert ms.store_history_source(store) == "alpaca"


def test_metric_store_mismatch_refuses_for_a_job(tmp_path, monkeypatch):
    store = _build_store(tmp_path, monkeypatch, "alpaca")
    assert ms.require_store_history_source(store, "alpaca") == "alpaca"
    with pytest.raises(HistorySourceMismatch, match="'alpaca' OHLCV history"):
        ms.require_store_history_source(store, "fmp")


def test_legacy_store_without_a_stamp_reads_as_fmp(tmp_path, monkeypatch):
    store = _build_store(tmp_path, monkeypatch, "fmp")
    path = os.path.join(store, "build_manifest.json")
    with open(path) as f:
        doc = json.load(f)
    for b in doc["builds"]:
        del b[OHLCV_HISTORY_SOURCE_KEY]
    with open(path, "w") as f:
        json.dump(doc, f)
    assert ms.store_history_source(store) == "fmp"
    assert ms.require_store_history_source(store, "fmp") == "fmp"       # an unstamped job on an unstamped store
    with pytest.raises(HistorySourceMismatch):
        ms.require_store_history_source(store, "alpaca")                # an alpaca job on a legacy store
    os.remove(path)                                                     # no manifest at all: legacy too
    assert ms.store_history_source(store) == "fmp"


def test_extending_a_store_of_the_other_history_refuses(tmp_path, monkeypatch):
    store = _build_store(tmp_path, monkeypatch, "fmp")
    with pytest.raises(HistorySourceMismatch):
        _build_store(tmp_path, monkeypatch, "alpaca", store=store)


def test_build_store_requires_the_source():
    with pytest.raises(TypeError):
        ms.build_store("d", "k", "2026-01-03", "2026-03-28", market_cap_min=0, price_min=0, volume_min=0,
                       ohlcv_get=lambda s, e: None)


def test_historical_screener_asks_the_registry_for_its_history(monkeypatch):
    import ba2_providers
    import importlib
    mod = importlib.import_module("ba2_providers.screener.FMPHistoricalScreenerProvider")
    cls = mod.FMPHistoricalScreenerProvider
    asked = []
    monkeypatch.setattr(ba2_providers, "get_provider", lambda kind, name: asked.append((kind, name)) or object())
    monkeypatch.setattr(mod, "get_app_setting", lambda k: "key")
    cls()._ohlcv_provider()
    cls(ohlcv_history_source="alpaca")._ohlcv_provider()
    assert asked == [("ohlcv", "fmp"), ("ohlcv", "alpaca_hist")]
    with pytest.raises(Exception):
        cls(ohlcv_history_source="yahoo")


def test_parity_cache_dir_follows_the_panels_source(tmp_path, monkeypatch):
    from ba2_providers.screener import live_parity as lp
    seen = []
    monkeypatch.setattr(lp, "_five_min_day", lambda d, s, day: seen.append(os.path.basename(d)) or None)

    class _P:
        symbols = ["A"]
        arrays = {"o": [[1.0]]}
        manifest = {OHLCV_HISTORY_SOURCE_KEY: "alpaca"}

        def pos(self, day):
            return 0
    lp.make_now(_P(), "2024-01-02")(range(1))
    _P.manifest = {}
    lp.make_now(_P(), "2024-01-02")(range(1))
    assert seen == ["AlpacaHistoryOHLCVProvider", "FMPOHLCVProvider"]
