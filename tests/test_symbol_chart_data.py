"""Tests for build_chart_data (SYMBOL360 price chart data assembly helper).

Uses fake ohlcv_provider / indicator_calc objects rather than the real
YFinanceDataProvider / PandasIndicatorCalc, so these tests exercise only the
assembly logic in ba2_trade_platform.ui.components.symbol_chart_data.
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from ba2_trade_platform.ui.components.symbol_chart_data import (
    DEFAULT_INDICATORS,
    build_chart_data,
)


def _make_ohlcv_df():
    dates = pd.date_range("2026-01-01", periods=5, freq="D")
    return pd.DataFrame({
        "Date": dates,
        "Open": [10.0, 11.0, 12.0, 13.0, 14.0],
        "High": [10.5, 11.5, 12.5, 13.5, 14.5],
        "Low": [9.5, 10.5, 11.5, 12.5, 13.5],
        "Close": [10.2, 11.2, 12.2, 13.2, 14.2],
        "Volume": [1000, 1100, 1200, 1300, 1400],
    })


def _indicator_dict(n=5, start="2026-01-01"):
    dates = pd.date_range(start, periods=n, freq="D")
    return {
        "dates": [d.isoformat() for d in dates],
        "values": [float(i) for i in range(n)],
    }


class FakeOhlcvProvider:
    def __init__(self, df):
        self._df = df
        self.calls = []

    def get_ohlcv_data(self, symbol, start_date, end_date, interval):
        self.calls.append(dict(symbol=symbol, start_date=start_date,
                                end_date=end_date, interval=interval))
        return self._df


class FakeIndicatorCalc:
    """Real PandasIndicatorCalc exposes get_indicator(...), not
    get_indicator_data(...) -- verified by reading the source directly."""

    def __init__(self, results=None, raises_for=None, malformed_for=None):
        self.results = results or {}
        self.raises_for = raises_for or {}
        self.malformed_for = malformed_for or set()
        self.calls = []

    def get_indicator(self, symbol, indicator, start_date, end_date, interval, format_type):
        self.calls.append(dict(symbol=symbol, indicator=indicator, start_date=start_date,
                                end_date=end_date, interval=interval, format_type=format_type))
        if indicator in self.raises_for:
            raise self.raises_for[indicator]
        if indicator in self.malformed_for:
            return {"unexpected": "shape"}
        return self.results.get(indicator, _indicator_dict())


def test_default_indicators_are_real_pandasindicatorcalc_keys():
    # PandasIndicatorCalc.SUPPORTED_INDICATOR_KEYS uses these exact strings
    # (stockstats-driven, fixed periods) -- "sma_200"/"rsi_14" from the plan's
    # sketch are NOT valid keys and would raise ValueError if used.
    assert DEFAULT_INDICATORS == ["close_200_sma", "rsi"]


def test_normal_case_returns_tz_aware_price_and_indicators():
    ohlcv = FakeOhlcvProvider(_make_ohlcv_df())
    calc = FakeIndicatorCalc()

    price_data, indicators_data = build_chart_data("AAPL", ohlcv, calc)

    assert isinstance(price_data.index, pd.DatetimeIndex)
    assert price_data.index.tz is not None
    for col in ("Open", "High", "Low", "Close", "Volume"):
        assert col in price_data.columns

    # indicator display names: DEFAULT_INDICATORS run through
    # indicator.replace("_", " ").title()
    assert set(indicators_data.keys()) == {"Close 200 Sma", "Rsi"}
    for name, df in indicators_data.items():
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.tz is not None
        assert list(df.columns) == [name]


def test_get_indicator_called_with_real_datetimes_not_strings():
    # validate_date_range (ba2_common.core.provider_utils) accesses
    # start_date.tzinfo directly -- a string start_date raises AttributeError.
    ohlcv = FakeOhlcvProvider(_make_ohlcv_df())
    calc = FakeIndicatorCalc()

    build_chart_data("AAPL", ohlcv, calc)

    assert len(calc.calls) == len(DEFAULT_INDICATORS)
    for call in calc.calls:
        assert isinstance(call["start_date"], datetime)
        assert isinstance(call["end_date"], datetime)
        assert call["format_type"] == "dict"
        assert call["symbol"] == "AAPL"


def test_empty_ohlcv_dataframe_returns_empty_indicators_and_passthrough_price():
    empty_df = pd.DataFrame()
    ohlcv = FakeOhlcvProvider(empty_df)
    calc = FakeIndicatorCalc()

    price_data, indicators_data = build_chart_data("AAPL", ohlcv, calc)

    assert price_data is empty_df
    assert indicators_data == {}
    # Indicator calc must not even be called once we know there's no price data
    assert calc.calls == []


def test_none_ohlcv_response_returns_none_and_empty_dict():
    ohlcv = FakeOhlcvProvider(None)
    calc = FakeIndicatorCalc()

    price_data, indicators_data = build_chart_data("AAPL", ohlcv, calc)

    assert price_data is None
    assert indicators_data == {}


def test_indicator_raising_is_skipped_but_other_indicators_still_returned():
    ohlcv = FakeOhlcvProvider(_make_ohlcv_df())
    calc = FakeIndicatorCalc(raises_for={"close_200_sma": ValueError("boom")})

    price_data, indicators_data = build_chart_data("AAPL", ohlcv, calc)

    assert "Close 200 Sma" not in indicators_data
    assert "Rsi" in indicators_data


def test_indicator_unexpected_exception_type_is_also_skipped_not_propagated():
    # Deliberate design choice: a single bad overlay indicator must never take
    # down the whole chart, so the guard is a broad `except Exception`, not a
    # narrow ValueError catch. Prove a totally unrelated exception type
    # (TypeError, not something get_indicator is documented to raise) is
    # still swallowed for that one indicator, and its neighbor is unaffected.
    ohlcv = FakeOhlcvProvider(_make_ohlcv_df())
    calc = FakeIndicatorCalc(raises_for={"rsi": TypeError("totally unexpected")})

    price_data, indicators_data = build_chart_data("AAPL", ohlcv, calc)

    assert "Rsi" not in indicators_data
    assert "Close 200 Sma" in indicators_data


def test_malformed_indicator_result_is_skipped():
    ohlcv = FakeOhlcvProvider(_make_ohlcv_df())
    calc = FakeIndicatorCalc(malformed_for={"close_200_sma"})

    price_data, indicators_data = build_chart_data("AAPL", ohlcv, calc)

    assert "Close 200 Sma" not in indicators_data
    assert "Rsi" in indicators_data


def test_indicator_mismatched_dates_values_length_is_skipped_not_propagated():
    # A response that PASSES the "dates"/"values" key check but has
    # mismatched lengths used to blow up pd.DataFrame construction OUTSIDE
    # the try/except -- discarding price_data along with it. That
    # DataFrame(...)/to_datetime(...) call must now be inside the same
    # failure-isolation unit as the fetch itself.
    ohlcv = FakeOhlcvProvider(_make_ohlcv_df())
    calc = FakeIndicatorCalc(results={
        "close_200_sma": {"dates": [f"2026-01-0{i+1}" for i in range(5)],
                          "values": [1.0, 2.0, 3.0]},  # 5 dates, 3 values
    })

    price_data, indicators_data = build_chart_data("AAPL", ohlcv, calc)

    assert price_data is not None and not price_data.empty
    assert "Close 200 Sma" not in indicators_data
    assert "Rsi" in indicators_data


def test_price_data_with_date_column_and_naive_index_is_localized_to_utc():
    df = _make_ohlcv_df()  # has "Date" column, RangeIndex (not DatetimeIndex)
    ohlcv = FakeOhlcvProvider(df)
    calc = FakeIndicatorCalc()

    price_data, _ = build_chart_data("AAPL", ohlcv, calc)

    assert isinstance(price_data.index, pd.DatetimeIndex)
    assert price_data.index.name == "Date"
    assert str(price_data.index.tz) == "UTC"


def test_price_data_already_tz_aware_index_is_not_double_localized():
    df = _make_ohlcv_df()
    df = df.set_index("Date")
    df.index = df.index.tz_localize("UTC")
    ohlcv = FakeOhlcvProvider(df)
    calc = FakeIndicatorCalc()

    # Must not raise (tz_localize on an already-aware index raises TypeError)
    price_data, _ = build_chart_data("AAPL", ohlcv, calc)

    assert str(price_data.index.tz) == "UTC"


def test_lookback_days_and_interval_are_forwarded_to_ohlcv_provider():
    ohlcv = FakeOhlcvProvider(_make_ohlcv_df())
    calc = FakeIndicatorCalc()

    build_chart_data("MSFT", ohlcv, calc, lookback_days=30, interval="1h")

    assert len(ohlcv.calls) == 1
    call = ohlcv.calls[0]
    assert call["symbol"] == "MSFT"
    assert call["interval"] == "1h"
    delta = call["end_date"] - call["start_date"]
    assert delta.days == 30
