"""
Unit and integration tests for the Chronos foundation model service.

Unit tests (fast, no model download):
    cd backend
    ./venv/bin/python -m pytest tests/test_chronos_service.py -v -m "not slow"

Integration tests (requires model download, slow):
    cd backend
    ./venv/bin/python -m pytest tests/test_chronos_service.py -v -m "slow"

All tests:
    cd backend
    ./venv/bin/python -m pytest tests/test_chronos_service.py -v
"""

import sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.services.chronos_service import (
    CHRONOS_MODELS,
    CHRONOS_AVAILABLE,
    forecast_to_probabilities,
    list_available_models,
    get_model_info,
)


# ============================================================================
# Test Data Helpers
# ============================================================================

def create_price_series(n_samples: int = 200, seed: int = 42) -> pd.DataFrame:
    """Create a synthetic price series with Date and Close columns."""
    np.random.seed(seed)
    dates = pd.date_range(start='2024-01-01', periods=n_samples, freq='1h')

    # Random walk with drift
    returns = np.random.normal(0.0005, 0.01, n_samples)
    prices = 100.0 * np.exp(np.cumsum(returns))

    high = prices * (1 + np.abs(np.random.normal(0, 0.003, n_samples)))
    low = prices * (1 - np.abs(np.random.normal(0, 0.003, n_samples)))
    open_prices = prices * (1 + np.random.normal(0, 0.002, n_samples))
    volume = np.random.lognormal(mean=15, sigma=0.5, size=n_samples)

    return pd.DataFrame({
        'Date': dates,
        'Open': open_prices,
        'High': high,
        'Low': low,
        'Close': prices,
        'Volume': volume,
    })


def create_trending_series(direction: str = 'up', n_samples: int = 200, seed: int = 42) -> pd.DataFrame:
    """Create a clearly trending price series for predictable signal testing."""
    np.random.seed(seed)
    dates = pd.date_range(start='2024-01-01', periods=n_samples, freq='1h')

    if direction == 'up':
        drift = 0.005  # Strong upward drift
    else:
        drift = -0.005  # Strong downward drift

    returns = np.random.normal(drift, 0.002, n_samples)
    prices = 100.0 * np.exp(np.cumsum(returns))

    return pd.DataFrame({
        'Date': dates,
        'Open': prices * 0.999,
        'High': prices * 1.003,
        'Low': prices * 0.997,
        'Close': prices,
        'Volume': np.ones(n_samples) * 1000000,
    })


# ============================================================================
# Unit Tests (no model download required)
# ============================================================================

class TestChronosModelsRegistry:
    """Tests for the Chronos models registry."""

    def test_registry_has_models(self):
        assert len(CHRONOS_MODELS) > 0

    def test_chronos_2_in_registry(self):
        assert 'chronos-2' in CHRONOS_MODELS

    def test_model_entries_have_required_fields(self):
        required_fields = [
            'repo_id', 'params', 'description',
            'supports_covariates', 'max_context_length', 'max_prediction_length'
        ]
        for name, info in CHRONOS_MODELS.items():
            for field in required_fields:
                assert field in info, f"Model '{name}' missing field '{field}'"

    def test_chronos_2_supports_covariates(self):
        assert CHRONOS_MODELS['chronos-2']['supports_covariates'] is True

    def test_bolt_models_no_covariates(self):
        for name, info in CHRONOS_MODELS.items():
            if 'bolt' in name:
                assert info['supports_covariates'] is False, (
                    f"Bolt model '{name}' should not support covariates"
                )


class TestListAvailableModels:
    """Tests for list_available_models()."""

    def test_returns_list(self):
        models = list_available_models()
        assert isinstance(models, list)
        assert len(models) > 0

    def test_model_entries_have_name(self):
        models = list_available_models()
        for m in models:
            assert 'name' in m
            assert 'repo_id' in m
            assert 'description' in m

    def test_installed_field_matches_availability(self):
        models = list_available_models()
        for m in models:
            assert m['installed'] == CHRONOS_AVAILABLE


class TestGetModelInfo:
    """Tests for get_model_info()."""

    def test_valid_model(self):
        info = get_model_info('chronos-2')
        assert info['name'] == 'chronos-2'
        assert 'repo_id' in info
        assert 'installed' in info

    def test_invalid_model_raises(self):
        with pytest.raises(ValueError, match="Unknown Chronos model"):
            get_model_info('nonexistent-model')


class TestForecastToProbabilities:
    """Tests for forecast → probability signal conversion."""

    def test_price_up_gives_high_p_up(self):
        """When forecast is above current price, p_up should be > 0.5."""
        probs = forecast_to_probabilities(
            forecast_median=105.0,
            current_price=100.0,
            scale_factor=100.0,
        )
        assert probs.shape == (2,)
        assert probs[1] > 0.5  # p_up > 0.5
        assert probs[0] < 0.5  # p_down < 0.5

    def test_price_down_gives_low_p_up(self):
        """When forecast is below current price, p_up should be < 0.5."""
        probs = forecast_to_probabilities(
            forecast_median=95.0,
            current_price=100.0,
            scale_factor=100.0,
        )
        assert probs[1] < 0.5
        assert probs[0] > 0.5

    def test_equal_price_gives_even_split(self):
        """When forecast equals current price, probabilities should be 0.5/0.5."""
        probs = forecast_to_probabilities(
            forecast_median=100.0,
            current_price=100.0,
        )
        np.testing.assert_almost_equal(probs[0], 0.5)
        np.testing.assert_almost_equal(probs[1], 0.5)

    def test_probabilities_sum_to_one(self):
        """Probabilities must always sum to 1."""
        for forecast in [90.0, 95.0, 100.0, 105.0, 110.0, 200.0, 50.0]:
            probs = forecast_to_probabilities(forecast, 100.0)
            np.testing.assert_almost_equal(probs.sum(), 1.0)

    def test_probabilities_bounded(self):
        """Probabilities should be in [0, 1]."""
        for forecast in [0.01, 50.0, 100.0, 200.0, 1000.0]:
            probs = forecast_to_probabilities(forecast, 100.0)
            assert np.all(probs >= 0)
            assert np.all(probs <= 1)

    def test_zero_current_price_returns_even(self):
        """Edge case: zero current price should return 0.5/0.5."""
        probs = forecast_to_probabilities(105.0, 0.0)
        np.testing.assert_almost_equal(probs[0], 0.5)
        np.testing.assert_almost_equal(probs[1], 0.5)

    def test_scale_factor_affects_confidence(self):
        """Higher scale factor should give more extreme probabilities."""
        probs_low = forecast_to_probabilities(101.0, 100.0, scale_factor=10.0)
        probs_high = forecast_to_probabilities(101.0, 100.0, scale_factor=1000.0)
        # Higher scale = more confident
        assert probs_high[1] > probs_low[1]

    def test_symmetry(self):
        """Symmetric price moves should give symmetric probabilities."""
        probs_up = forecast_to_probabilities(110.0, 100.0, scale_factor=50.0)
        probs_down = forecast_to_probabilities(90.0, 100.0, scale_factor=50.0)
        # p_up for +10% should equal p_down for -10% (approximately)
        # Not exact due to log-like behavior of returns, but close
        np.testing.assert_almost_equal(probs_up[1], probs_down[0], decimal=1)


class TestRunChronosInferenceWithMock:
    """Tests for run_chronos_inference with mocked Chronos pipeline."""

    def _create_mock_pipeline(self, forecast_value: float = 101.0):
        """Create a mock Chronos pipeline that returns constant forecasts."""
        import torch

        mock_pipeline = MagicMock()

        def mock_predict(contexts, prediction_length=1):
            batch_size = len(contexts)
            num_quantiles = 21
            # Return constant forecast for all quantiles
            return torch.full(
                (batch_size, num_quantiles, prediction_length),
                forecast_value,
                dtype=torch.float32,
            )

        mock_pipeline.predict = mock_predict
        return mock_pipeline

    @patch('app.services.chronos_service.get_pipeline')
    def test_inference_returns_dict(self, mock_get_pipeline):
        """Inference should return a dict of timestamp -> probability array."""
        from app.services.chronos_service import run_chronos_inference

        mock_get_pipeline.return_value = self._create_mock_pipeline(101.0)
        df = create_price_series(200)

        result = run_chronos_inference(
            df=df,
            prediction_length=1,
            model_name='chronos-2',
            min_context_length=32,
        )

        assert isinstance(result, dict)
        assert len(result) > 0

    @patch('app.services.chronos_service.get_pipeline')
    def test_inference_output_format(self, mock_get_pipeline):
        """Each prediction should be a 2-element probability array."""
        from app.services.chronos_service import run_chronos_inference

        mock_get_pipeline.return_value = self._create_mock_pipeline(101.0)
        df = create_price_series(200)

        result = run_chronos_inference(
            df=df,
            prediction_length=1,
            model_name='chronos-2',
            min_context_length=32,
        )

        for ts, probs in result.items():
            assert isinstance(ts, pd.Timestamp)
            assert probs.shape == (2,), f"Expected shape (2,), got {probs.shape}"
            np.testing.assert_almost_equal(probs.sum(), 1.0)

    @patch('app.services.chronos_service.get_pipeline')
    def test_inference_prediction_count(self, mock_get_pipeline):
        """Number of predictions should equal (n_samples - min_context) / stride."""
        from app.services.chronos_service import run_chronos_inference

        mock_get_pipeline.return_value = self._create_mock_pipeline(101.0)
        n_samples = 200
        min_context = 64
        stride = 1
        df = create_price_series(n_samples)

        result = run_chronos_inference(
            df=df,
            prediction_length=1,
            model_name='chronos-2',
            min_context_length=min_context,
            stride=stride,
        )

        expected_count = len(range(min_context, n_samples, stride))
        assert len(result) == expected_count, (
            f"Expected {expected_count} predictions, got {len(result)}"
        )

    @patch('app.services.chronos_service.get_pipeline')
    def test_inference_with_stride(self, mock_get_pipeline):
        """Stride > 1 should reduce number of predictions."""
        from app.services.chronos_service import run_chronos_inference

        mock_get_pipeline.return_value = self._create_mock_pipeline(101.0)
        df = create_price_series(200)

        result_s1 = run_chronos_inference(
            df=df, prediction_length=1, model_name='chronos-2',
            min_context_length=64, stride=1,
        )
        result_s5 = run_chronos_inference(
            df=df, prediction_length=1, model_name='chronos-2',
            min_context_length=64, stride=5,
        )

        assert len(result_s5) < len(result_s1)

    @patch('app.services.chronos_service.get_pipeline')
    def test_inference_upward_forecast_signal(self, mock_get_pipeline):
        """Forecast above current price should give p_up > 0.5."""
        from app.services.chronos_service import run_chronos_inference

        # Forecast 5% above current prices
        mock_get_pipeline.return_value = self._create_mock_pipeline(105.0)
        df = create_price_series(200)
        # Set all Close prices to 100 for easy math
        df['Close'] = 100.0

        result = run_chronos_inference(
            df=df, prediction_length=1, model_name='chronos-2',
            min_context_length=32,
        )

        for ts, probs in result.items():
            assert probs[1] > 0.5, f"Expected p_up > 0.5, got {probs[1]}"

    @patch('app.services.chronos_service.get_pipeline')
    def test_inference_downward_forecast_signal(self, mock_get_pipeline):
        """Forecast below current price should give p_up < 0.5."""
        from app.services.chronos_service import run_chronos_inference

        mock_get_pipeline.return_value = self._create_mock_pipeline(95.0)
        df = create_price_series(200)
        df['Close'] = 100.0

        result = run_chronos_inference(
            df=df, prediction_length=1, model_name='chronos-2',
            min_context_length=32,
        )

        for ts, probs in result.items():
            assert probs[1] < 0.5, f"Expected p_up < 0.5, got {probs[1]}"

    @patch('app.services.chronos_service.get_pipeline')
    def test_inference_missing_target_column(self, mock_get_pipeline):
        """Should raise ValueError if target column is missing."""
        from app.services.chronos_service import run_chronos_inference

        mock_get_pipeline.return_value = self._create_mock_pipeline(101.0)
        df = create_price_series(200)
        df = df.drop(columns=['Close'])

        with pytest.raises(ValueError, match="Target column"):
            run_chronos_inference(df=df, model_name='chronos-2')

    @patch('app.services.chronos_service.get_pipeline')
    def test_inference_missing_date_column(self, mock_get_pipeline):
        """Should raise ValueError if Date column is missing."""
        from app.services.chronos_service import run_chronos_inference

        mock_get_pipeline.return_value = self._create_mock_pipeline(101.0)
        df = create_price_series(200)
        df = df.drop(columns=['Date'])

        with pytest.raises(ValueError, match="Date"):
            run_chronos_inference(df=df, model_name='chronos-2')

    @patch('app.services.chronos_service.get_pipeline')
    def test_inference_dataset_too_short(self, mock_get_pipeline):
        """Should raise ValueError if dataset is too short."""
        from app.services.chronos_service import run_chronos_inference

        mock_get_pipeline.return_value = self._create_mock_pipeline(101.0)
        df = create_price_series(10)  # Very short

        with pytest.raises(ValueError, match="too short"):
            run_chronos_inference(
                df=df, model_name='chronos-2', min_context_length=64,
            )

    @patch('app.services.chronos_service.get_pipeline')
    def test_inference_handles_nan_in_data(self, mock_get_pipeline):
        """Windows with NaN should be skipped, not crash."""
        from app.services.chronos_service import run_chronos_inference

        mock_get_pipeline.return_value = self._create_mock_pipeline(101.0)
        df = create_price_series(200)

        # Inject NaN in a few places
        df.loc[50, 'Close'] = np.nan
        df.loc[51, 'Close'] = np.nan

        result = run_chronos_inference(
            df=df, prediction_length=1, model_name='chronos-2',
            min_context_length=32,
        )

        # Should still produce predictions (skipping NaN windows)
        assert len(result) > 0
        # Fewer predictions than without NaN
        result_clean = run_chronos_inference(
            df=create_price_series(200), prediction_length=1,
            model_name='chronos-2', min_context_length=32,
        )
        assert len(result) <= len(result_clean)


class TestGetPipeline:
    """Tests for get_pipeline function."""

    def test_invalid_model_raises(self):
        from app.services.chronos_service import get_pipeline
        with pytest.raises(ValueError, match="Unknown Chronos model"):
            get_pipeline('nonexistent-model')

    @pytest.mark.skipif(not CHRONOS_AVAILABLE, reason="chronos-forecasting not installed")
    def test_not_available_without_library(self):
        # This test only makes sense if the library IS available
        # We just verify it doesn't raise RuntimeError when available
        from app.services.chronos_service import get_pipeline
        # Don't actually call it (would download model),
        # just verify the function exists and validates model names
        with pytest.raises(ValueError):
            get_pipeline('invalid')


class TestClearPipelineCache:
    """Tests for pipeline cache management."""

    def test_clear_cache(self):
        from app.services.chronos_service import _pipeline_cache, clear_pipeline_cache
        _pipeline_cache['test'] = 'dummy'
        clear_pipeline_cache()
        assert len(_pipeline_cache) == 0


# ============================================================================
# Integration Tests (require actual Chronos model download)
# ============================================================================

@pytest.mark.slow
@pytest.mark.skipif(not CHRONOS_AVAILABLE, reason="chronos-forecasting not installed")
class TestChronosInferenceIntegration:
    """Integration tests with real Chronos model.

    These tests download the actual model from HuggingFace on first run.
    They are slow and marked with @pytest.mark.slow.
    Run with: pytest -m slow
    """

    def test_real_inference_on_synthetic_data(self):
        """Run actual Chronos inference on synthetic price data."""
        from app.services.chronos_service import run_chronos_inference, clear_pipeline_cache

        df = create_price_series(200)

        # Use the smallest bolt model for speed
        result = run_chronos_inference(
            df=df,
            prediction_length=1,
            model_name='chronos-bolt-tiny',
            min_context_length=32,
        )

        assert len(result) > 0

        for ts, probs in result.items():
            assert probs.shape == (2,)
            np.testing.assert_almost_equal(probs.sum(), 1.0)
            assert np.all(probs > 0)
            assert np.all(probs < 1)

        clear_pipeline_cache()

    def test_real_inference_different_prediction_lengths(self):
        """Test with different prediction lengths."""
        from app.services.chronos_service import run_chronos_inference, clear_pipeline_cache

        df = create_price_series(200)

        for pred_len in [1, 3, 5]:
            result = run_chronos_inference(
                df=df,
                prediction_length=pred_len,
                model_name='chronos-bolt-tiny',
                min_context_length=32,
            )
            assert len(result) > 0, f"No predictions for prediction_length={pred_len}"

        clear_pipeline_cache()

    def test_real_inference_with_chronos_2(self):
        """Test with the full Chronos-2 model (120M params)."""
        from app.services.chronos_service import run_chronos_inference, clear_pipeline_cache

        df = create_price_series(100)

        result = run_chronos_inference(
            df=df,
            prediction_length=1,
            model_name='chronos-2',
            min_context_length=32,
            stride=5,  # Use stride to speed up test
        )

        assert len(result) > 0

        for ts, probs in result.items():
            assert probs.shape == (2,)
            np.testing.assert_almost_equal(probs.sum(), 1.0)

        clear_pipeline_cache()


# ============================================================================
# Backtest Integration Tests (with mocked Chronos)
# ============================================================================

class TestChronosBacktestIntegration:
    """Test Chronos integration with the backtest handler using mocked inference."""

    def _create_mock_chronos_model(self):
        """Create a mock TrainedModel with Chronos model_type."""
        mock_model = MagicMock()
        mock_model.model_type = 'chronos:chronos-2'
        mock_model.hyperparameters = {
            'chronos_model': 'chronos-2',
            'prediction_length': 1,
        }
        mock_model.file_path = None
        mock_model.prediction_mode = 'regression'
        mock_model.threshold = 0.5
        mock_model.normalization_params = None
        return mock_model

    @patch('app.services.chronos_service.run_chronos_inference')
    @patch('app.services.chronos_service.CHRONOS_AVAILABLE', True)
    def test_chronos_backtest_routes_correctly(self, mock_inference):
        """Verify that Chronos model type triggers the Chronos backtest path."""
        from app.services.backtest_handler import run_backtest

        df = create_price_series(200)

        # Set up mock predictions
        dates = pd.to_datetime(df['Date'])
        pred_lookup = {pd.Timestamp(d): np.array([0.3, 0.7]) for d in dates[64:]}
        mock_inference.return_value = pred_lookup

        model = self._create_mock_chronos_model()
        strategy_params = {'initialTpPercent': 5, 'initialSlPercent': 2}

        # Simple buy condition: model predicts up with >60% confidence
        buy_conditions = {
            'type': 'condition',
            'field': 'model:probability_1',
            'operator': '>',
            'value': 0.6,
        }

        result = run_backtest(
            model=model,
            pred_df=df,
            exec_df=df,
            strategy_params=strategy_params,
            buy_entry_conditions=buy_conditions,
        )

        # Should have called chronos inference
        mock_inference.assert_called_once()
        # Should return valid backtest results
        assert 'total_trades' in result
        assert 'equity_curve' in result


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-m', 'not slow'])
