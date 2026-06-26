"""Single backtest screener universe = per-bar metric_store gate (no static-union lookahead).

Replaces the retired ScreenerHistoryCache static-union path: a single BT with
``universe.mode=='screener'`` now (1) uses the metric_store symbol UNION as the candidate
``enabled_instruments`` and (2) sets ``screener_runtime`` so the engine gates entries PER BAR via
``metric_store.screen_universe_as_of`` (point-in-time). This test proves both, and that a name
which only qualifies LATE is NOT admitted on an early bar (the lookahead the old union path had).
"""
import pandas as pd
import pytest

from ba2_providers.screener import metric_store as ms
from app.services.backtest.daily_backtest_handler import (
    _build_config,
    _metric_store_settings,
)
from app.services.backtest.daily_engine import _screened_symbols_for_bar
from datetime import datetime, timezone


def _build_store(tmp_path) -> str:
    """Tiny 2-symbol store: A qualifies on both scan dates; B only on the LATE one (mcap jumps)."""
    rows = [
        # date, symbol, market_cap, price, volume, relative_volume, price_drop_pct, weinstein_stage
        {"date": "2024-01-01", "symbol": "A", "market_cap": 2e9, "price": 50, "volume": 1e6,
         "relative_volume": 1.5, "price_drop_pct": 0.0, "weinstein_stage": 2, "sector": "X", "close": 50},
        {"date": "2024-02-01", "symbol": "A", "market_cap": 2e9, "price": 52, "volume": 1e6,
         "relative_volume": 1.5, "price_drop_pct": 0.0, "weinstein_stage": 2, "sector": "X", "close": 52},
        {"date": "2024-01-01", "symbol": "B", "market_cap": 5e8, "price": 10, "volume": 1e6,
         "relative_volume": 1.5, "price_drop_pct": 0.0, "weinstein_stage": 2, "sector": "X", "close": 10},
        {"date": "2024-02-01", "symbol": "B", "market_cap": 2e9, "price": 12, "volume": 1e6,
         "relative_volume": 1.5, "price_drop_pct": 0.0, "weinstein_stage": 2, "sector": "X", "close": 12},
    ]
    store = str(tmp_path / "ms")
    ms.write_partitions(store, pd.DataFrame(rows))
    ms.clear_store_memo()
    return store


def test_metric_store_settings_maps_and_filters_keys():
    # Strips the screener_ prefix and keeps the recognized subset. float_min/float_max ARE
    # supported (point-in-time free-float gate); price_drop_days IS supported (the optimizable
    # lookback window Y that selects the precomputed price_drop_pct_<Y> column). Unknown keys drop.
    out = _metric_store_settings({
        "screener_market_cap_min": 1e9,
        "market_cap_max": 0,
        "screener_price_drop_days": 5,   # supported -> mapped (lookback window)
        "screener_float_min": 1e6,       # supported -> mapped
        "screener_float_max": 5e8,       # supported -> mapped
        "sort_metric": "market_cap",
        "unknown_key": 1,                # unsupported -> dropped
        "missing": None,                 # None -> dropped
    })
    assert out == {"market_cap_min": 1e9, "market_cap_max": 0, "sort_metric": "market_cap",
                   "price_drop_days": 5, "float_min": 1e6, "float_max": 5e8}


def test_single_bt_screener_union_runtime_and_no_lookahead(tmp_path):
    store = _build_store(tmp_path)
    payload = {
        "backtest_id": 1,
        "start_date": "2024-01-01",
        "end_date": "2024-03-01",
        "experts": ["FMPRating"],
        "initial_capital": 100000.0,
        "commission": 1.0,
        "slippage": 0.0,
        "fill_model": "next_bar_open",
        "seed": 42,
        "universe": {
            "mode": "screener",
            "screener_store": store,
            "screener_settings": {"market_cap_min": 1e9},
        },
    }
    cfg = _build_config(payload)

    # (1) candidate universe = the store's symbol UNION (superset; engine loads OHLCV for all).
    assert cfg["enabled_instruments"] == ["A", "B"]

    # (2) screener_runtime wired for the per-bar gate (mapped, unprefixed settings).
    rt = cfg["screener_runtime"]
    assert rt is not None
    assert rt["store"] == store
    assert rt["settings"] == {"market_cap_min": 1e9}
    assert rt["cadence_days"] == 7

    # (3) NO lookahead: B only qualifies on 2024-02-01, so an early bar must NOT admit it.
    early = _screened_symbols_for_bar(rt, datetime(2024, 1, 15, tzinfo=timezone.utc))
    late = _screened_symbols_for_bar(rt, datetime(2024, 2, 15, tzinfo=timezone.utc))
    assert "A" in early and "B" not in early   # was admitted by the old static-union (the bug)
    assert "A" in late and "B" in late


def test_screener_mode_defaults_to_store_dir(tmp_path, monkeypatch):
    """Omitting universe.screener_store now DEFAULTS to SCREENER_STORE_DIR (where
    build-screener-metrics writes), so a single screener BT 'just works' against the built store
    without the caller passing the path."""
    store = _build_store(tmp_path)
    import ba2_common.config as _cfg
    monkeypatch.setattr(_cfg, "SCREENER_STORE_DIR", store)
    payload = {
        "backtest_id": 2, "start_date": "2024-01-01", "end_date": "2024-03-01",
        "experts": ["FMPRating"], "initial_capital": 100000.0, "commission": 1.0,
        "slippage": 0.0, "fill_model": "next_bar_open", "seed": 42,
        "universe": {"mode": "screener", "screener_settings": {"market_cap_min": 1e9}},  # no store
    }
    cfg = _build_config(payload)
    assert cfg["enabled_instruments"] == ["A", "B"]            # resolved from the default store
    assert cfg["screener_runtime"]["store"] == store


def test_screener_mode_missing_store_dir_errors(tmp_path, monkeypatch):
    """If neither an explicit store nor the default dir exists, fail with an actionable message."""
    import ba2_common.config as _cfg
    monkeypatch.setattr(_cfg, "SCREENER_STORE_DIR", str(tmp_path / "does-not-exist"))
    payload = {
        "backtest_id": 2, "start_date": "2024-01-01", "end_date": "2024-03-01",
        "experts": ["FMPRating"], "initial_capital": 100000.0, "commission": 1.0,
        "slippage": 0.0, "fill_model": "next_bar_open", "seed": 42,
        "universe": {"mode": "screener", "screener_settings": {"market_cap_min": 1e9}},
    }
    with pytest.raises(ValueError, match="metric_store not found"):
        _build_config(payload)


def test_static_mode_unaffected(tmp_path):
    payload = {
        "backtest_id": 3, "start_date": "2024-01-01", "end_date": "2024-03-01",
        "experts": ["FMPRating"], "initial_capital": 100000.0, "commission": 1.0,
        "slippage": 0.0, "fill_model": "next_bar_open", "seed": 42,
        "enabled_instruments": ["AAPL", "MSFT"],
        "universe": {"mode": "static", "symbols": ["AAPL", "MSFT"]},
    }
    cfg = _build_config(payload)
    assert cfg["enabled_instruments"] == ["AAPL", "MSFT"]
    assert cfg["screener_runtime"] is None  # engine gate no-op for static runs
