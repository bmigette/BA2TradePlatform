"""
Tests for PennyMomentumTrader conditions module.

Covers: validation (valid/invalid conditions, missing params, unknown types),
condition set validation, composite evaluation (all/any), time conditions,
percent above/below entry, and get_condition_types_for_llm output.
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from ba2_trade_platform.modules.experts.PennyMomentumTrader.conditions import (
    CONDITION_TYPES,
    ConditionEvaluator,
    get_condition_types_for_llm,
    validate_condition,
    validate_condition_set,
)


# ---------------------------------------------------------------------------
# Mock OHLCV provider
# ---------------------------------------------------------------------------

def _make_ohlcv_df(closes, highs=None, lows=None, opens=None, volumes=None):
    """Build a minimal OHLCV DataFrame from a list of close prices."""
    n = len(closes)
    if highs is None:
        highs = [c + 0.5 for c in closes]
    if lows is None:
        lows = [c - 0.5 for c in closes]
    if opens is None:
        opens = [c - 0.1 for c in closes]
    if volumes is None:
        volumes = [1000] * n
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


class MockOHLCVProvider:
    """Mock OHLCV provider returning configurable data per (symbol, interval)."""

    def __init__(self, data_map=None):
        """
        Args:
            data_map: dict of (symbol, interval) -> DataFrame or just a DataFrame
                      for all requests.
        """
        self._data_map = data_map or {}
        self._default_df = None

    def set_default(self, df):
        self._default_df = df

    def get_ohlcv_data(self, symbol, interval="1d", lookback_days=30, **kwargs):
        key = (symbol, interval)
        if key in self._data_map:
            return self._data_map[key]
        if self._default_df is not None:
            return self._default_df
        # Return empty DataFrame
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestValidateCondition:
    def test_valid_condition(self):
        ok, msg = validate_condition({"type": "price_above", "value": 5.0})
        assert ok is True
        assert msg == ""

    def test_valid_condition_with_multiple_params(self):
        ok, msg = validate_condition({
            "type": "rsi_between", "min": 30, "max": 70, "period": 14, "timeframe": "1d"
        })
        assert ok is True

    def test_missing_type(self):
        ok, msg = validate_condition({"value": 5.0})
        assert ok is False
        assert "type" in msg.lower()

    def test_unknown_type(self):
        ok, msg = validate_condition({"type": "banana_indicator"})
        assert ok is False
        assert "Unknown" in msg

    def test_missing_required_param(self):
        ok, msg = validate_condition({"type": "price_above"})
        assert ok is False
        assert "value" in msg

    def test_missing_one_of_several_params(self):
        ok, msg = validate_condition({"type": "rsi_above", "threshold": 70})
        assert ok is False
        assert "period" in msg or "timeframe" in msg

    def test_not_a_dict(self):
        ok, msg = validate_condition("not_a_dict")
        assert ok is False

    def test_empty_dict(self):
        ok, msg = validate_condition({})
        assert ok is False


class TestValidateConditionSet:
    def test_valid_full_set(self):
        cs = {
            "entry": {"all": [
                {"type": "price_above_ema", "period": 9, "timeframe": "15m"},
                {"type": "rsi_above", "threshold": 50, "period": 14, "timeframe": "15m"},
            ]},
            "stop_loss": {"any": [
                {"type": "percent_below_entry", "percent": 5},
            ]},
            "take_profit": [
                {
                    "condition": {"type": "percent_above_entry", "percent": 10},
                    "exit_pct": 50,
                },
            ],
        }
        ok, errors = validate_condition_set(cs)
        assert ok is True
        assert errors == []

    def test_invalid_entry_condition(self):
        cs = {
            "entry": {"all": [
                {"type": "unknown_thing"},
            ]},
        }
        ok, errors = validate_condition_set(cs)
        assert ok is False
        assert len(errors) >= 1
        assert "Unknown" in errors[0]

    def test_invalid_take_profit_missing_exit_pct(self):
        cs = {
            "take_profit": [
                {"condition": {"type": "percent_above_entry", "percent": 10}},
            ],
        }
        ok, errors = validate_condition_set(cs)
        assert ok is False
        assert any("exit_pct" in e for e in errors)

    def test_invalid_take_profit_missing_condition(self):
        cs = {
            "take_profit": [
                {"exit_pct": 50},
            ],
        }
        ok, errors = validate_condition_set(cs)
        assert ok is False
        assert any("condition" in e for e in errors)

    def test_nested_composite(self):
        cs = {
            "entry": {"all": [
                {"any": [
                    {"type": "price_above", "value": 1.0},
                    {"type": "price_below", "value": 0.5},
                ]},
                {"type": "rsi_above", "threshold": 50, "period": 14, "timeframe": "1d"},
            ]},
        }
        ok, errors = validate_condition_set(cs)
        assert ok is True

    def test_not_a_dict(self):
        ok, errors = validate_condition_set("bad")
        assert ok is False

    def test_empty_set_is_valid(self):
        ok, errors = validate_condition_set({})
        assert ok is True
        assert errors == []

    def test_take_profit_not_a_list(self):
        cs = {"take_profit": "bad"}
        ok, errors = validate_condition_set(cs)
        assert ok is False

    def test_take_profit_item_not_a_dict(self):
        cs = {"take_profit": ["bad"]}
        ok, errors = validate_condition_set(cs)
        assert ok is False

    def test_take_profit_composite_condition(self):
        cs = {
            "take_profit": [
                {
                    "condition": {"all": [
                        {"type": "percent_above_entry", "percent": 10},
                        {"type": "time_after", "time": "14:00"},
                    ]},
                    "exit_pct": 100,
                },
            ],
        }
        ok, errors = validate_condition_set(cs)
        assert ok is True


# ---------------------------------------------------------------------------
# get_condition_types_for_llm tests
# ---------------------------------------------------------------------------

class TestGetConditionTypesForLLM:
    def test_returns_string(self):
        result = get_condition_types_for_llm()
        assert isinstance(result, str)

    def test_contains_all_types(self):
        result = get_condition_types_for_llm()
        for name in CONDITION_TYPES:
            assert name in result

    def test_contains_composition_info(self):
        result = get_condition_types_for_llm()
        assert "all" in result
        assert "any" in result
        assert "AND" in result
        assert "OR" in result

    def test_contains_example(self):
        result = get_condition_types_for_llm()
        assert "price_above_ema" in result


# ---------------------------------------------------------------------------
# ConditionEvaluator tests
# ---------------------------------------------------------------------------

class TestConditionEvaluatorBasic:
    def setup_method(self):
        self.provider = MockOHLCVProvider()
        self.evaluator = ConditionEvaluator(self.provider)

    def test_clear_cache(self):
        self.evaluator._indicator_cache["foo"] = "bar"
        self.evaluator.clear_cache()
        assert self.evaluator._indicator_cache == {}

    def test_unknown_condition_type_returns_false(self):
        result = self.evaluator.evaluate_single(
            {"type": "nonexistent"}, "AAPL"
        )
        assert result is False

    def test_evaluate_single_with_error_returns_false(self):
        # Provider that raises
        provider = MagicMock()
        provider.get_ohlcv_data.side_effect = Exception("boom")
        evaluator = ConditionEvaluator(provider)
        result = evaluator.evaluate_single(
            {"type": "price_above", "value": 5.0}, "AAPL"
        )
        assert result is False


class TestCompositeEvaluation:
    def setup_method(self):
        closes = [10.0] * 50
        df = _make_ohlcv_df(closes)
        self.provider = MockOHLCVProvider()
        self.provider.set_default(df)
        self.evaluator = ConditionEvaluator(self.provider)

    def test_all_true(self):
        """All conditions met -> True."""
        conditions = {"all": [
            {"type": "price_above", "value": 5.0},
            {"type": "price_below", "value": 15.0},
        ]}
        assert self.evaluator.evaluate(conditions, "TEST") is True

    def test_all_one_false(self):
        """One condition not met in 'all' -> False."""
        conditions = {"all": [
            {"type": "price_above", "value": 5.0},
            {"type": "price_above", "value": 15.0},  # 10 is not > 15
        ]}
        assert self.evaluator.evaluate(conditions, "TEST") is False

    def test_any_one_true(self):
        """At least one condition met in 'any' -> True."""
        conditions = {"any": [
            {"type": "price_above", "value": 15.0},  # False
            {"type": "price_below", "value": 15.0},  # True
        ]}
        assert self.evaluator.evaluate(conditions, "TEST") is True

    def test_any_all_false(self):
        """No conditions met in 'any' -> False."""
        conditions = {"any": [
            {"type": "price_above", "value": 15.0},
            {"type": "price_above", "value": 20.0},
        ]}
        assert self.evaluator.evaluate(conditions, "TEST") is False

    def test_nested_composite(self):
        """Nested all/any evaluation."""
        conditions = {"all": [
            {"any": [
                {"type": "price_above", "value": 15.0},  # False
                {"type": "price_below", "value": 15.0},  # True -> any=True
            ]},
            {"type": "price_above", "value": 5.0},  # True
        ]}
        assert self.evaluator.evaluate(conditions, "TEST") is True

    def test_single_condition_without_composite(self):
        """A single condition dict without all/any wrapper."""
        condition = {"type": "price_above", "value": 5.0}
        assert self.evaluator.evaluate(condition, "TEST") is True


class TestGetConditionStatus:
    def test_returns_dict_with_all_conditions(self):
        closes = [10.0] * 50
        df = _make_ohlcv_df(closes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        evaluator = ConditionEvaluator(provider)

        conditions = {"all": [
            {"type": "price_above", "value": 5.0},
            {"type": "price_below", "value": 15.0},
        ]}
        status = evaluator.get_condition_status(conditions, "TEST")
        assert isinstance(status, dict)
        assert len(status) == 2
        assert all(v is True for v in status.values())


# ---------------------------------------------------------------------------
# Percent-based condition tests
# ---------------------------------------------------------------------------

class TestPercentConditions:
    def setup_method(self):
        closes = [10.0] * 50
        df = _make_ohlcv_df(closes)
        self.provider = MockOHLCVProvider()
        self.provider.set_default(df)
        self.evaluator = ConditionEvaluator(self.provider)

    def test_percent_above_entry_true(self):
        # Current price is 10.0, entry was 8.0 -> 25% above entry
        closes = [10.0] * 50
        df = _make_ohlcv_df(closes)
        self.provider.set_default(df)
        result = self.evaluator.evaluate_single(
            {"type": "percent_above_entry", "percent": 10}, "TEST", entry_price=8.0
        )
        assert result is True

    def test_percent_above_entry_false(self):
        # Current price is 10.0, entry was 10.0 -> 0% above entry
        result = self.evaluator.evaluate_single(
            {"type": "percent_above_entry", "percent": 10}, "TEST", entry_price=10.0
        )
        assert result is False

    def test_percent_below_entry_true(self):
        # Current price is 10.0, entry was 12.0 -> ~16.7% below entry
        closes = [10.0] * 50
        df = _make_ohlcv_df(closes)
        self.provider.set_default(df)
        result = self.evaluator.evaluate_single(
            {"type": "percent_below_entry", "percent": 5}, "TEST", entry_price=12.0
        )
        assert result is True

    def test_percent_below_entry_false(self):
        # Current price is 10.0, entry was 10.0 -> 0% below entry
        result = self.evaluator.evaluate_single(
            {"type": "percent_below_entry", "percent": 5}, "TEST", entry_price=10.0
        )
        assert result is False

    def test_percent_no_entry_price(self):
        result = self.evaluator.evaluate_single(
            {"type": "percent_above_entry", "percent": 10}, "TEST", entry_price=None
        )
        assert result is False

    def test_percent_below_no_entry_price(self):
        result = self.evaluator.evaluate_single(
            {"type": "percent_below_entry", "percent": 5}, "TEST", entry_price=None
        )
        assert result is False


# ---------------------------------------------------------------------------
# Time-based condition tests
# ---------------------------------------------------------------------------

class TestTimeConditions:
    def setup_method(self):
        self.provider = MockOHLCVProvider()
        self.evaluator = ConditionEvaluator(self.provider, market_timezone="US/Eastern")

    @patch("ba2_trade_platform.modules.experts.PennyMomentumTrader.conditions.datetime")
    def test_time_after_true(self, mock_dt):
        import pytz
        tz = pytz.timezone("US/Eastern")
        mock_dt.now.return_value = datetime(2026, 3, 16, 14, 30, tzinfo=tz)
        result = self.evaluator.evaluate_single(
            {"type": "time_after", "time": "10:00"}, "TEST"
        )
        assert result is True

    @patch("ba2_trade_platform.modules.experts.PennyMomentumTrader.conditions.datetime")
    def test_time_after_false(self, mock_dt):
        import pytz
        tz = pytz.timezone("US/Eastern")
        mock_dt.now.return_value = datetime(2026, 3, 16, 9, 0, tzinfo=tz)
        result = self.evaluator.evaluate_single(
            {"type": "time_after", "time": "10:00"}, "TEST"
        )
        assert result is False

    @patch("ba2_trade_platform.modules.experts.PennyMomentumTrader.conditions.datetime")
    def test_time_before_true(self, mock_dt):
        import pytz
        tz = pytz.timezone("US/Eastern")
        mock_dt.now.return_value = datetime(2026, 3, 16, 9, 0, tzinfo=tz)
        result = self.evaluator.evaluate_single(
            {"type": "time_before", "time": "10:00"}, "TEST"
        )
        assert result is True

    @patch("ba2_trade_platform.modules.experts.PennyMomentumTrader.conditions.datetime")
    def test_time_before_false(self, mock_dt):
        import pytz
        tz = pytz.timezone("US/Eastern")
        mock_dt.now.return_value = datetime(2026, 3, 16, 14, 30, tzinfo=tz)
        result = self.evaluator.evaluate_single(
            {"type": "time_before", "time": "10:00"}, "TEST"
        )
        assert result is False


# ---------------------------------------------------------------------------
# Price/indicator condition tests
# ---------------------------------------------------------------------------

class TestPriceConditions:
    def test_price_above(self):
        df = _make_ohlcv_df([10.0] * 20)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        assert ev.evaluate_single({"type": "price_above", "value": 9.0}, "X") is True
        assert ev.evaluate_single({"type": "price_above", "value": 11.0}, "X") is False

    def test_price_below(self):
        df = _make_ohlcv_df([10.0] * 20)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        assert ev.evaluate_single({"type": "price_below", "value": 11.0}, "X") is True
        assert ev.evaluate_single({"type": "price_below", "value": 9.0}, "X") is False


class TestEMAConditions:
    def test_price_above_ema(self):
        # Rising prices -> last price above EMA
        closes = list(range(1, 51))  # 1 to 50
        df = _make_ohlcv_df([float(c) for c in closes])
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "price_above_ema", "period": 9, "timeframe": "1d"}, "X"
        )
        assert result is True

    def test_price_below_ema(self):
        # Falling prices -> last price below EMA
        closes = list(range(50, 0, -1))  # 50 to 1
        df = _make_ohlcv_df([float(c) for c in closes])
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "price_below_ema", "period": 9, "timeframe": "1d"}, "X"
        )
        assert result is True


class TestSMAConditions:
    def test_price_above_sma(self):
        closes = list(range(1, 51))
        df = _make_ohlcv_df([float(c) for c in closes])
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "price_above_sma", "period": 9, "timeframe": "1d"}, "X"
        )
        assert result is True

    def test_price_below_sma(self):
        closes = list(range(50, 0, -1))
        df = _make_ohlcv_df([float(c) for c in closes])
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "price_below_sma", "period": 9, "timeframe": "1d"}, "X"
        )
        assert result is True


class TestRSIConditions:
    def test_rsi_above(self):
        # Strongly rising prices -> high RSI
        closes = [float(i) for i in range(1, 52)]
        df = _make_ohlcv_df(closes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "rsi_above", "threshold": 50, "period": 14, "timeframe": "1d"}, "X"
        )
        assert result is True

    def test_rsi_below(self):
        # Strongly falling prices -> low RSI
        closes = [float(i) for i in range(51, 0, -1)]
        df = _make_ohlcv_df(closes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "rsi_below", "threshold": 50, "period": 14, "timeframe": "1d"}, "X"
        )
        assert result is True

    def test_rsi_between(self):
        # Mix of up and down -> RSI likely between 30-70
        closes = [10.0, 11.0, 10.5, 11.5, 10.0, 11.0, 10.5, 11.5,
                  10.0, 11.0, 10.5, 11.5, 10.0, 11.0, 10.5, 11.5,
                  10.0, 11.0, 10.5, 11.5, 10.0, 11.0, 10.5, 11.5,
                  10.0, 11.0, 10.5, 11.5, 10.0, 11.0]
        df = _make_ohlcv_df(closes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "rsi_between", "min": 20, "max": 80, "period": 14, "timeframe": "1d"}, "X"
        )
        assert result is True


class TestVWAPConditions:
    def test_price_above_vwap(self):
        # Last close well above typical price average
        closes = [10.0] * 49 + [20.0]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        volumes = [1000] * 50
        df = _make_ohlcv_df(closes, highs=highs, lows=lows, volumes=volumes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "price_above_vwap", "timeframe": "1m"}, "X"
        )
        assert result is True

    def test_price_below_vwap(self):
        # Last close well below typical price average
        closes = [20.0] * 49 + [10.0]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        volumes = [1000] * 50
        df = _make_ohlcv_df(closes, highs=highs, lows=lows, volumes=volumes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "price_below_vwap", "timeframe": "1m"}, "X"
        )
        assert result is True


class TestMACDConditions:
    def _make_crossover_data(self, bullish=True):
        """Create price data that produces a MACD crossover."""
        n = 60
        if bullish:
            # Downtrend then sharp upturn
            closes = [50.0 - i * 0.3 for i in range(40)] + [50.0 + i * 2.0 for i in range(20)]
        else:
            # Uptrend then sharp downturn
            closes = [10.0 + i * 0.3 for i in range(40)] + [25.0 - i * 2.0 for i in range(20)]
        return _make_ohlcv_df(closes)

    def test_macd_bullish_cross(self):
        df = self._make_crossover_data(bullish=True)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        # This tests that the code runs; actual crossover detection depends on data
        result = ev.evaluate_single(
            {"type": "macd_bullish_cross", "timeframe": "1d"}, "X"
        )
        assert isinstance(result, bool)

    def test_macd_bearish_cross(self):
        df = self._make_crossover_data(bullish=False)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "macd_bearish_cross", "timeframe": "1d"}, "X"
        )
        assert isinstance(result, bool)


class TestEMACrossConditions:
    def test_ema_cross_above(self):
        # Sharp upturn at the end
        closes = [10.0] * 30 + [10.0 + i * 2.0 for i in range(20)]
        df = _make_ohlcv_df(closes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "ema_cross_above", "fast_period": 5, "slow_period": 20, "timeframe": "1d"}, "X"
        )
        assert isinstance(result, bool)

    def test_ema_cross_below(self):
        # Sharp downturn at the end
        closes = [50.0] * 30 + [50.0 - i * 2.0 for i in range(20)]
        df = _make_ohlcv_df(closes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "ema_cross_below", "fast_period": 5, "slow_period": 20, "timeframe": "1d"}, "X"
        )
        assert isinstance(result, bool)


class TestVolumeConditions:
    def test_volume_above_avg(self):
        # Last bar has very high volume
        volumes = [1000] * 49 + [10000]
        df = _make_ohlcv_df([10.0] * 50, volumes=volumes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "volume_above_avg", "multiplier": 2.0, "window": 20}, "X"
        )
        assert result is True

    def test_volume_above_avg_false(self):
        volumes = [1000] * 50
        df = _make_ohlcv_df([10.0] * 50, volumes=volumes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "volume_above_avg", "multiplier": 2.0, "window": 20}, "X"
        )
        assert result is False

    def test_volume_spike(self):
        # Last 5 minutes have very high volume
        volumes = [100] * 45 + [5000] * 5
        df = _make_ohlcv_df([10.0] * 50, volumes=volumes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "volume_spike", "multiplier": 2.0, "minutes": 5}, "X"
        )
        assert result is True


class TestRVOLConditions:
    """Tests for the rvol_above condition type."""

    def test_rvol_above_true(self):
        """RVOL should be high when today's volume is much larger than average."""
        # 20 historical days with 1000 volume each, today has 5000
        volumes = [1000] * 20 + [5000]
        df = _make_ohlcv_df([10.0] * 21, volumes=volumes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)

        # Mock datetime to be mid-session (12:30 ET = 3 hours in = ~46% of day)
        with patch("ba2_trade_platform.modules.experts.PennyMomentumTrader.conditions.datetime") as mock_dt:
            import pytz
            et = pytz.timezone("US/Eastern")
            mock_now = et.localize(datetime(2026, 3, 19, 12, 30, 0))
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            result = ev.evaluate_single(
                {"type": "rvol_above", "threshold": 2.0}, "X"
            )
            # raw_rvol = 5000/1000 = 5.0, fraction ~= 180/390 ~= 0.46
            # rvol = 5.0 / 0.46 ~= 10.8 >> 2.0
            assert result is True

    def test_rvol_above_false(self):
        """RVOL should be low when today's volume is below average."""
        # 20 historical days with 1000 volume each, today has only 200
        volumes = [1000] * 20 + [200]
        df = _make_ohlcv_df([10.0] * 21, volumes=volumes)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)

        with patch("ba2_trade_platform.modules.experts.PennyMomentumTrader.conditions.datetime") as mock_dt:
            import pytz
            et = pytz.timezone("US/Eastern")
            mock_now = et.localize(datetime(2026, 3, 19, 12, 30, 0))
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            result = ev.evaluate_single(
                {"type": "rvol_above", "threshold": 2.0}, "X"
            )
            # raw_rvol = 200/1000 = 0.2, fraction ~= 0.46
            # rvol = 0.2 / 0.46 ~= 0.43 < 2.0
            assert result is False

    def test_rvol_above_no_data(self):
        """RVOL should return False when no data is available."""
        provider = MockOHLCVProvider()  # no data
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "rvol_above", "threshold": 1.5}, "X"
        )
        assert result is False

    def test_rvol_validation(self):
        """rvol_above should validate correctly."""
        valid, msg = validate_condition({"type": "rvol_above", "threshold": 2.0})
        assert valid is True

        valid, msg = validate_condition({"type": "rvol_above"})
        assert valid is False
        assert "threshold" in msg


class TestOpeningRangeBreakout:
    def test_breakout_true(self):
        # First 5 bars high = 10.5, current price = 15.0
        closes = [10.0] * 5 + [15.0] * 20
        highs = [10.5] * 5 + [15.5] * 20
        df = _make_ohlcv_df(closes, highs=highs)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "opening_range_breakout", "minutes": 5}, "X"
        )
        assert result is True

    def test_breakout_false(self):
        # First 5 bars high = 20.5, current price = 10.0
        closes = [20.0] * 5 + [10.0] * 20
        highs = [20.5] * 5 + [10.5] * 20
        df = _make_ohlcv_df(closes, highs=highs)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single(
            {"type": "opening_range_breakout", "minutes": 5}, "X"
        )
        assert result is False


class TestCaching:
    def test_ohlcv_data_is_cached(self):
        df = _make_ohlcv_df([10.0] * 20)
        provider = MockOHLCVProvider()
        provider.set_default(df)
        ev = ConditionEvaluator(provider)

        # First call
        ev.evaluate_single({"type": "price_above", "value": 5.0}, "X")
        cache_size = len(ev._indicator_cache)
        assert cache_size > 0

        # Clear and verify empty
        ev.clear_cache()
        assert len(ev._indicator_cache) == 0

    def test_empty_provider_returns_false(self):
        provider = MockOHLCVProvider()  # no data
        ev = ConditionEvaluator(provider)
        result = ev.evaluate_single({"type": "price_above", "value": 5.0}, "X")
        assert result is False
