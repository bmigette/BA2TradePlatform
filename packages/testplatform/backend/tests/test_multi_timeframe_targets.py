"""
Tests for multi-timeframe target calculations.

Tests that targets can be calculated on a higher timeframe and aligned
back to the base timeframe for training.
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


class TestTimeframeResampling:
    """Test OHLCV resampling functions."""

    def test_resample_15m_to_1h(self):
        """Test resampling 15-minute data to 1-hour."""
        from app.services.indicators import resample_ohlcv_to_timeframe

        # Create 15-minute OHLCV data
        dates = pd.date_range(
            start='2024-01-01 09:00:00',
            periods=16,  # 4 hours of 15m data
            freq='15min'
        )
        df = pd.DataFrame({
            'Date': dates,
            'Open': np.arange(100, 116),
            'High': np.arange(101, 117),
            'Low': np.arange(99, 115),
            'Close': np.arange(100.5, 116.5),
            'Volume': np.random.randint(1000, 5000, size=16)
        })

        resampled = resample_ohlcv_to_timeframe(df, '1h', source_timeframe='15m')

        # Should have 4 hourly bars
        assert len(resampled) == 4
        # First bar's Open should be the first 15m bar's Open
        assert resampled['Open'].iloc[0] == 100
        # First bar's High should be max of first 4 bars
        assert resampled['High'].iloc[0] == max([101, 102, 103, 104])
        # First bar's Low should be min of first 4 bars
        assert resampled['Low'].iloc[0] == min([99, 100, 101, 102])
        # First bar's Close should be last of first 4 bars
        assert resampled['Close'].iloc[0] == 103.5

    def test_resample_30m_to_4h(self):
        """Test resampling 30-minute data to 4-hour."""
        from app.services.indicators import resample_ohlcv_to_timeframe

        dates = pd.date_range(
            start='2024-01-01 08:00:00',
            periods=24,  # 12 hours of 30m data
            freq='30min'
        )
        df = pd.DataFrame({
            'Date': dates,
            'Open': np.arange(100, 124),
            'High': np.arange(105, 129),
            'Low': np.arange(95, 119),
            'Close': np.arange(102, 126),
            'Volume': np.ones(24) * 1000
        })

        resampled = resample_ohlcv_to_timeframe(df, '4h', source_timeframe='30m')

        # Should have 3 4-hour bars
        assert len(resampled) == 3

    def test_resample_to_lower_timeframe_raises(self):
        """Cannot resample to a lower timeframe."""
        from app.services.indicators import resample_ohlcv_to_timeframe

        dates = pd.date_range(start='2024-01-01', periods=10, freq='1h')
        df = pd.DataFrame({
            'Date': dates,
            'Open': np.ones(10),
            'High': np.ones(10),
            'Low': np.ones(10),
            'Close': np.ones(10),
            'Volume': np.ones(10)
        })

        with pytest.raises(ValueError, match="Cannot resample"):
            resample_ohlcv_to_timeframe(df, '15m', source_timeframe='1h')


class TestTimeframeAlignment:
    """Test alignment of higher timeframe data to lower timeframe."""

    def test_align_1h_to_15m(self):
        """Test aligning 1-hour data back to 15-minute base."""
        from app.services.indicators import align_higher_timeframe_to_lower

        # Lower timeframe (15m) data
        lower_dates = pd.date_range(
            start='2024-01-01 09:00:00',
            periods=8,  # 2 hours of 15m data
            freq='15min'
        )
        lower_df = pd.DataFrame({
            'Date': lower_dates,
            'Close': np.arange(100, 108)
        })

        # Higher timeframe (1h) data
        higher_dates = pd.date_range(
            start='2024-01-01 09:00:00',
            periods=2,
            freq='1h'
        )
        higher_df = pd.DataFrame({
            'Date': higher_dates,
            'indicator': [10.0, 20.0]
        })

        aligned = align_higher_timeframe_to_lower(lower_df, higher_df, ['indicator'])

        # All 15m bars in first hour should have value 10
        assert aligned['indicator'].iloc[0] == 10.0
        assert aligned['indicator'].iloc[1] == 10.0
        assert aligned['indicator'].iloc[2] == 10.0
        assert aligned['indicator'].iloc[3] == 10.0

        # All 15m bars in second hour should have value 20
        assert aligned['indicator'].iloc[4] == 20.0
        assert aligned['indicator'].iloc[5] == 20.0
        assert aligned['indicator'].iloc[6] == 20.0
        assert aligned['indicator'].iloc[7] == 20.0


class TestMultiTimeframeIndicators:
    """Test multi-timeframe indicator calculations."""

    def test_rsi_multi_timeframe(self):
        """Test RSI calculated on 1h timeframe aligned to 15m."""
        from app.services.indicators import IndicatorService

        # Create 15-minute data (enough for RSI calculation)
        dates = pd.date_range(
            start='2024-01-01 00:00:00',
            periods=120,  # 30 hours
            freq='15min'
        )

        # Create price data with some trend
        prices = 100 + np.cumsum(np.random.randn(120) * 0.5)

        df = pd.DataFrame({
            'Date': dates,
            'Open': prices,
            'High': prices + 0.5,
            'Low': prices - 0.5,
            'Close': prices,
            'Volume': np.ones(120) * 1000
        })

        service = IndicatorService()
        indicators = [{'type': 'rsi', 'period': 14}]

        results = service.calculate_indicators_multi_timeframe(
            df, indicators, '1h', source_timeframe='15m'
        )

        # Should have RSI column with timeframe suffix
        assert 'rsi_14_1h' in results

        # Should have same length as original df
        assert len(results['rsi_14_1h']) == len(df)

        # RSI should be between 0 and 100 where valid
        valid_rsi = results['rsi_14_1h'].dropna()
        assert all(0 <= v <= 100 for v in valid_rsi)

    def test_zigzag_multi_timeframe(self):
        """Test ZigZag calculated on 4h timeframe aligned to 1h."""
        from app.services.indicators import IndicatorService

        # Create 1-hour data
        dates = pd.date_range(
            start='2024-01-01 00:00:00',
            periods=100,
            freq='1h'
        )

        # Create price data with clear swings
        t = np.linspace(0, 4 * np.pi, 100)
        prices = 100 + 10 * np.sin(t) + np.random.randn(100) * 0.5

        df = pd.DataFrame({
            'Date': dates,
            'Open': prices,
            'High': prices + 1,
            'Low': prices - 1,
            'Close': prices,
            'Volume': np.ones(100) * 1000
        })

        service = IndicatorService()
        indicators = [{'type': 'zigzag', 'deviation_pct': 5.0}]

        results = service.calculate_indicators_multi_timeframe(
            df, indicators, '4h', source_timeframe='1h'
        )

        # Should have zigzag column with timeframe suffix
        assert 'zigzag_5.0_4h' in results

        # Should have same length as original df
        assert len(results['zigzag_5.0_4h']) == len(df)


class TestMultiTimeframeTargets:
    """Test multi-timeframe target calculations."""

    def test_trend_reversal_target_multi_timeframe(self):
        """Test trend reversal target calculated on higher timeframe."""
        from app.services.darts_models import PredictionTargetService

        # Create 15-minute data
        dates = pd.date_range(
            start='2024-01-01 00:00:00',
            periods=200,
            freq='15min'
        )

        # Create price data
        prices = 100 + np.cumsum(np.random.randn(200) * 0.5)

        df = pd.DataFrame({
            'Date': dates,
            'Open': prices,
            'High': prices + 0.5,
            'Low': prices - 0.5,
            'Close': prices,
            'Volume': np.ones(200) * 1000
        })

        service = PredictionTargetService()

        # Target with 1h timeframe on 15m data
        targets_config = [{
            'type': 'trend_reversal',
            'indicator': 'rsi',
            'indicatorParams': {'period': 14},
            'threshold': 30,
            'direction': 'bullish',
            'timeframe': '1h'
        }]

        results = service.calculate_all_targets(df, targets_config, dataset_timeframe='15m')

        assert len(results) == 1
        result = results[0]

        # Column name should include timeframe
        assert '1h' in result['columnName']

        # Should have data aligned to 15m (200 rows)
        assert len(result['data']) == 200

        # Category should be binary classification
        assert result['category'] == 'binary_classification'

    def test_target_without_timeframe_uses_dataset_timeframe(self):
        """Target without explicit timeframe uses dataset's base timeframe."""
        from app.services.darts_models import PredictionTargetService

        dates = pd.date_range(
            start='2024-01-01 00:00:00',
            periods=100,
            freq='1h'
        )

        prices = 100 + np.cumsum(np.random.randn(100) * 0.5)

        df = pd.DataFrame({
            'Date': dates,
            'Open': prices,
            'High': prices + 0.5,
            'Low': prices - 0.5,
            'Close': prices,
            'Volume': np.ones(100) * 1000
        })

        service = PredictionTargetService()

        # Target WITHOUT timeframe field
        targets_config = [{
            'type': 'directional',
            'direction': 'up',
            'horizon': 5
        }]

        results = service.calculate_all_targets(df, targets_config, dataset_timeframe='1h')

        assert len(results) == 1
        result = results[0]

        # Column name should NOT include timeframe suffix (same as dataset)
        assert '1h' not in result['columnName']
        assert 'directional' in result['columnName'] and 'up' in result['columnName']

    def test_same_timeframe_no_resampling(self):
        """When target timeframe equals dataset timeframe, no resampling."""
        from app.services.darts_models import PredictionTargetService

        dates = pd.date_range(
            start='2024-01-01 00:00:00',
            periods=100,
            freq='1h'
        )

        prices = 100 + np.cumsum(np.random.randn(100) * 0.5)

        df = pd.DataFrame({
            'Date': dates,
            'Open': prices,
            'High': prices + 0.5,
            'Low': prices - 0.5,
            'Close': prices,
            'Volume': np.ones(100) * 1000
        })

        service = PredictionTargetService()

        # Target with same timeframe as dataset
        targets_config = [{
            'type': 'directional',
            'direction': 'up',
            'horizon': 5,
            'timeframe': '1h'  # Same as dataset
        }]

        results = service.calculate_all_targets(df, targets_config, dataset_timeframe='1h')

        assert len(results) == 1
        result = results[0]

        # No timeframe suffix when same as dataset
        assert 'directional' in result['columnName'] and 'up' in result['columnName']
        # Should NOT have timeframe suffix since it's same as dataset
        assert not result['columnName'].endswith('_1h')


class TestTimeframeValidation:
    """Test timeframe validation and edge cases."""

    def test_unknown_timeframe_raises(self):
        """Unknown timeframe should raise error."""
        from app.services.indicators import resample_ohlcv_to_timeframe

        dates = pd.date_range(start='2024-01-01', periods=10, freq='1h')
        df = pd.DataFrame({
            'Date': dates,
            'Open': np.ones(10),
            'High': np.ones(10),
            'Low': np.ones(10),
            'Close': np.ones(10),
            'Volume': np.ones(10)
        })

        with pytest.raises(ValueError, match="Unknown target timeframe"):
            resample_ohlcv_to_timeframe(df, 'invalid_tf')

    def test_insufficient_data_after_resample(self):
        """Not enough data after resampling should be handled gracefully."""
        from app.services.indicators import IndicatorService

        # Only 2 15m bars - not enough for 1h bar
        dates = pd.date_range(
            start='2024-01-01 09:00:00',
            periods=2,
            freq='15min'
        )
        df = pd.DataFrame({
            'Date': dates,
            'Open': [100, 101],
            'High': [101, 102],
            'Low': [99, 100],
            'Close': [100.5, 101.5],
            'Volume': [1000, 1000]
        })

        service = IndicatorService()
        indicators = [{'type': 'rsi', 'period': 14}]

        # Should return empty dict, not crash
        results = service.calculate_indicators_multi_timeframe(
            df, indicators, '1h', source_timeframe='15m'
        )

        # Empty or with NaN values
        assert isinstance(results, dict)
