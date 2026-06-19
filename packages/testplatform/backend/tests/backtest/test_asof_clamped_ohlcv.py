"""AsOfClampedOHLCVProvider: caps every OHLCV fetch at the backtest clock so indicator/ATR
calcs (which fetch with end_date=datetime.now()) never see future bars during a backtest.
"""
from datetime import datetime, timezone

from app.services.backtest.price_source import AsOfPriceSource, AsOfClampedOHLCVProvider

CLK = datetime(2024, 1, 8, tzinfo=timezone.utc)


class _Rec:
    def __init__(self):
        self.end = []

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        self.end.append(end_date)
        return None

    def other_method(self):
        return "delegated"


def _ps(clock=CLK):
    ps = AsOfPriceSource(ohlcv_provider=None)
    if clock is not None:
        ps.set_clock(clock)
    return ps


def test_future_end_clamped_to_clock():
    rec = _Rec()
    AsOfClampedOHLCVProvider(rec, _ps()).get_ohlcv_data("AAPL", end_date=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert rec.end[-1] == CLK


def test_none_end_becomes_clock():
    rec = _Rec()
    AsOfClampedOHLCVProvider(rec, _ps()).get_ohlcv_data("AAPL")
    assert rec.end[-1] == CLK


def test_past_end_untouched():
    rec = _Rec()
    past = datetime(2023, 12, 1, tzinfo=timezone.utc)
    AsOfClampedOHLCVProvider(rec, _ps()).get_ohlcv_data("AAPL", end_date=past)
    assert rec.end[-1] == past


def test_naive_future_end_clamped():
    rec = _Rec()
    AsOfClampedOHLCVProvider(rec, _ps()).get_ohlcv_data("AAPL", end_date=datetime(2026, 6, 1))
    assert rec.end[-1] == CLK


def test_no_clock_passthrough():
    rec = _Rec()
    future = datetime(2026, 1, 1, tzinfo=timezone.utc)
    AsOfClampedOHLCVProvider(rec, _ps(clock=None)).get_ohlcv_data("AAPL", end_date=future)
    assert rec.end[-1] == future  # no clamp when the clock is unset


def test_delegates_other_attributes():
    assert AsOfClampedOHLCVProvider(_Rec(), _ps()).other_method() == "delegated"
