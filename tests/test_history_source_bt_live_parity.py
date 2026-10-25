"""BT/live parity of the history source (plan 2026-10-24-history-source-task11, Task 10, items 4 and 5).

The live bundle of an ``alpaca`` account and the backtest bundle (``history_registry_key("alpaca")``, no keys) read the SAME
cache files through the SAME provider class, so a deterministic indicator over the same window is identical; and the latest
bar of a live read is a FINISHED session at 09:30 and 10:00 ET alike.
"""
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import native_cache, ohlcv_final_bars
from ba2_common.core.backtest_context import LiveProviderBundle
from ba2_common.core.market_calendar import regular_session_dates
from ba2_common.core.ohlcv_history import history_registry_key
from ba2_trade_platform.modules.dataproviders import get_provider

FOLDER = "AlpacaHistoryOHLCVProvider"
LAST = date(2026, 10, 16)                                               # Friday: the file's last (final) session


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    root = tmp_path / FOLDER
    (root / "_meta").mkdir(parents=True)
    days = pd.DatetimeIndex([pd.Timestamp(d) for d in regular_session_dates(date(2026, 6, 1), LAST)])
    rng = np.random.default_rng(7)
    close = 50 + np.cumsum(rng.normal(0, 0.8, len(days)))
    frame = pd.DataFrame({"Date": days, "Open": close - 0.1, "High": close + 0.6, "Low": close - 0.7, "Close": close,
                          "Volume": 1000, "effective_date": days})
    frame.to_parquet(root / "AAA_1d.parquet", index=False)
    return root


def atr(bundle, end_date=None):
    out = bundle.indicators().get_indicator("AAA", "atr", end_date=end_date, lookback_days=80, interval="1d",
                                            format_type="dict", period=14)
    return [v for v in out["values"] if v is not None][-1]


def test_atr_through_a_live_alpaca_bundle_equals_the_backtest_bundle(cache, monkeypatch):
    monkeypatch.setattr(ohlcv_final_bars, "now_utc", lambda: datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc))
    live = LiveProviderBundle(get_provider, ohlcv_name="alpaca_hist", ohlcv_kwargs={"api_key": "k", "api_secret": "s"})
    assert live.ohlcv().live is True
    bt = LiveProviderBundle(get_provider, ohlcv_name=history_registry_key("alpaca"))
    assert bt.ohlcv().live is False
    assert atr(live) == pytest.approx(atr(bt, end_date=datetime(2026, 10, 19, 13, 30)), rel=0, abs=0)   # a backtest pins its decision time


@pytest.mark.parametrize("hour_utc,minute", [(13, 30), (14, 0)])         # 09:30 and 10:00 ET (EDT)
def test_the_latest_bar_of_a_live_read_is_a_finished_session(cache, monkeypatch, hour_utc, minute):
    now = datetime(2026, 10, 19, hour_utc, minute, tzinfo=timezone.utc)
    monkeypatch.setattr(ohlcv_final_bars, "now_utc", lambda: now)
    assert ohlcv_final_bars.last_final_session_day("AAA", now) == LAST     # Monday's own session is not final
    calls = []
    prov = get_provider("ohlcv", "alpaca_hist", api_key="k", api_secret="s")
    prov._fetcher = type("F", (), {"daily": lambda self, *a: calls.append(a), "intraday_5min": lambda self, *a: calls.append(a)})()
    df = prov.get_ohlcv_data("AAA", lookback_days=60, interval="1d")
    assert pd.to_datetime(df["Date"]).max().date() == LAST and calls == []   # nothing newer is final, nothing is fetched
