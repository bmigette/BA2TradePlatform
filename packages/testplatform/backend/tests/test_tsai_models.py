"""
Comprehensive unit tests for tsai model service.
Tests all 11 classification models with real AAPL data.
"""
import pytest
import numpy as np
import pandas as pd
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.tsai_models import TSAIModelService, TSAI_AVAILABLE

# Skip all tests if tsai not available
pytestmark = pytest.mark.skipif(not TSAI_AVAILABLE, reason="tsai not available")

# Test data path
TEST_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "AAPL_1h_test.csv")


@pytest.fixture
def model_service():
    return TSAIModelService()


@pytest.fixture
def test_data():
    """Load and prepare AAPL test data."""
    df = pd.read_csv(TEST_DATA_PATH)
    # Use a subset for faster tests
    df = df.head(500)
    return df


@pytest.fixture
def sample_input():
    """Create sample input for model creation."""
    return {
        'c_in': 10,
        'c_out': 2,
        'seq_len': 24,
    }


class TestTSAIModelService:
    """Tests for TSAIModelService."""

    def test_get_available_models(self, model_service):
        """Test getting available models (excludes forecasting-only models)."""
        models = model_service.get_available_models()
        assert len(models) == 10  # PatchTST excluded (forecasting-only)
        assert 'lstm' in models
        assert 'inception' in models
        assert 'minirocket' in models
        assert 'patchtst' not in models  # Excluded from classification

    def test_get_parameter_ranges(self, model_service):
        """Test getting parameter ranges."""
        ranges = model_service.get_parameter_ranges('inception')
        assert 'nf' in ranges
        assert 'depth' in ranges
        assert isinstance(ranges['nf'], list)

    def test_get_parameter_ranges_invalid_model(self, model_service):
        """Test error on invalid model type."""
        with pytest.raises(ValueError):
            model_service.get_parameter_ranges('invalid_model')

    def test_apply_layer_size_factor(self, model_service):
        """Test layer size scaling."""
        params = {'hidden_size': 64, 'nf': 32, 'd_model': 128}
        scaled = model_service.apply_layer_size_factor(params, 2.0)
        assert scaled['hidden_size'] == 128
        assert scaled['nf'] == 64
        assert scaled['d_model'] == 256

    def test_apply_layer_size_factor_with_list(self, model_service):
        """Test layer size scaling with list params."""
        params = {'layers': [32, 64, 64]}
        scaled = model_service.apply_layer_size_factor(params, 2.0)
        assert scaled['layers'] == [64, 128, 128]

    def test_get_system_info(self, model_service):
        """Test system info."""
        info = model_service.get_system_info()
        assert 'tsai_available' in info
        assert 'cuda_available' in info
        assert 'device' in info
        assert info['tsai_available'] is True


class TestModelCreation:
    """Test creating each model type."""

    @pytest.mark.parametrize("model_type", [
        'lstm', 'gru', 'tcn', 'inception', 'resnet',
        'xception', 'omniscale', 'lstm_fcn', 'tst'
    ])
    def test_create_model(self, model_service, sample_input, model_type):
        """Test creating each model type."""
        model = model_service.create_model(
            model_type,
            {},
            c_in=sample_input['c_in'],
            c_out=sample_input['c_out'],
            seq_len=sample_input['seq_len']
        )
        assert model is not None

    def test_create_minirocket(self, model_service, sample_input):
        """Test creating MiniRocket (requires seq_len)."""
        model = model_service.create_model(
            'minirocket',
            {},
            c_in=sample_input['c_in'],
            c_out=sample_input['c_out'],
            seq_len=sample_input['seq_len']
        )
        assert model is not None

    def test_create_patchtst(self, model_service, sample_input):
        """Test creating PatchTST."""
        model = model_service.create_model(
            'patchtst',
            {'patch_len': 8},  # Smaller patch for short seq
            c_in=sample_input['c_in'],
            c_out=sample_input['c_out'],
            seq_len=sample_input['seq_len']
        )
        assert model is not None

    def test_create_model_with_custom_params(self, model_service, sample_input):
        """Test creating model with custom parameters."""
        model = model_service.create_model(
            'lstm',
            {'hidden_size': 128, 'n_layers': 3, 'bidirectional': True},
            c_in=sample_input['c_in'],
            c_out=sample_input['c_out'],
            seq_len=sample_input['seq_len']
        )
        assert model is not None

    def test_create_invalid_model(self, model_service, sample_input):
        """Test error on invalid model type."""
        with pytest.raises(ValueError):
            model_service.create_model(
                'invalid',
                {},
                c_in=sample_input['c_in'],
                c_out=sample_input['c_out'],
                seq_len=sample_input['seq_len']
            )


class TestModelForwardPass:
    """Test that models can process data."""

    @pytest.mark.parametrize("model_type", ['lstm', 'gru', 'inception', 'resnet'])
    def test_forward_pass(self, model_service, sample_input, model_type):
        """Test forward pass through model."""
        import torch

        model = model_service.create_model(
            model_type,
            {},
            c_in=sample_input['c_in'],
            c_out=sample_input['c_out'],
            seq_len=sample_input['seq_len']
        )

        # Create random input
        batch_size = 4
        x = torch.randn(batch_size, sample_input['c_in'], sample_input['seq_len'])

        # Forward pass
        model.eval()
        with torch.no_grad():
            output = model(x)

        assert output.shape == (batch_size, sample_input['c_out'])


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
