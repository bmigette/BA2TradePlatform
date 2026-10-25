"""The InsufficientHistory guard of PandasIndicatorCalc: honours the caller's period, and leaves a long history unchanged."""
from datetime import datetime

import pandas as pd
import pytest
from stockstats import wrap

from ba2_common.core.ohlcv_history import InsufficientHistory
from ba2_providers.indicators.PandasIndicatorCalc import PandasIndicatorCalc, required_bars


class FrameProvider:
    def __init__(self, n):
        d = pd.bdate_range(end="2026-10-23", periods=n)
        close = pd.Series(range(n), dtype=float) * 0.1 + 50
        self.frame = pd.DataFrame({"Date": d, "Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 1000})

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        return self.frame.copy()


def test_required_bars_honours_the_period():
    assert required_bars("atr") == 14 and required_bars("atr", 20) == 20 and required_bars("atr", 5) == 14
    assert required_bars("close_200_sma", 14) == 200 and required_bars("rsi", 30) == 30 and required_bars("nope") == 0


def test_atr_with_period_20_needs_20_bars_not_14():
    calc = PandasIndicatorCalc(FrameProvider(16))
    calc.get_indicator("AAA", "atr", end_date=datetime(2026, 10, 23), lookback_days=10, format_type="dict")
    with pytest.raises(InsufficientHistory, match="needs 20"):
        calc.get_indicator("AAA", "atr", end_date=datetime(2026, 10, 23), lookback_days=10, format_type="dict", period=20)


@pytest.mark.parametrize("indicator", ["close_200_sma", "close_50_sma", "rsi", "atr", "macd", "boll"])
def test_a_long_history_is_computed_exactly_as_stockstats_does(indicator):
    prov = FrameProvider(400)
    out = PandasIndicatorCalc(prov).get_indicator("AAA", indicator, end_date=datetime(2026, 10, 23), lookback_days=30,
                                                   format_type="dict")
    df = wrap(prov.frame.copy())
    expected = [round(float(v), 4) for v in df[indicator].tail(len(out["values"]))]
    assert out["values"] and out["values"] == expected
