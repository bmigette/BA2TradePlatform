"""No-lookahead: PandasIndicatorCalc must fetch OHLCV up to the REQUESTED end_date, not
wall-clock now(). Fetching to now() in a backtest leaks future bars into ATR/indicators
used for position sizing / rule conditions.
"""
from datetime import datetime

import pandas as pd

from ba2_providers.indicators.PandasIndicatorCalc import PandasIndicatorCalc


def _naive(x):
    t = pd.to_datetime(x)
    return t.tz_localize(None) if t.tzinfo is not None else t


class _RecordingOHLCV:
    """Records the end_date it is asked for; returns a static synthetic daily frame."""

    def __init__(self):
        self.asked_end = []
        n = 1600  # ~2021-08 .. 2026 daily
        dates = pd.date_range("2021-08-01", periods=n, freq="D", tz="UTC")
        self.df = pd.DataFrame({
            "Date": dates,
            "Open": [100.0 + (i % 7) for i in range(n)],
            "High": [102.0 + (i % 7) for i in range(n)],
            "Low": [98.0 + (i % 7) for i in range(n)],
            "Close": [100.0 + (i % 5) for i in range(n)],
            "Volume": [1000] * n,
        })

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        self.asked_end.append(end_date)
        return self.df.copy()


def test_get_indicator_fetches_to_requested_end_not_now():
    rec = _RecordingOHLCV()
    calc = PandasIndicatorCalc(ohlcv_provider=rec)
    as_of = datetime(2024, 1, 8)
    calc.get_indicator("AAPL", "atr", end_date=as_of, lookback_days=90, format_type="dict")
    assert rec.asked_end, "provider was never queried"
    for e in rec.asked_end:
        days_off = abs((_naive(e) - _naive(as_of)).days)
        assert days_off <= 1, (
            f"indicator fetched OHLCV to {e} but as_of was {as_of} ({days_off}d off) -> future leak"
        )
