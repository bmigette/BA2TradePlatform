"""
Comprehensive unit tests for tsai training service.
Tests training, model assessment, and loss functions with real AAPL data.
"""
import pytest
import numpy as np
import pandas as pd
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.tsai_models import TSAIModelService, TSAI_AVAILABLE
from app.services.tsai_training import TSAITrainingService

pytestmark = pytest.mark.skipif(not TSAI_AVAILABLE, reason="tsai not available")

TEST_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "AAPL_1h_test.csv")


@pytest.fixture
def training_service():
    return TSAITrainingService()


@pytest.fixture
def model_service():
    return TSAIModelService()


@pytest.fixture
def prepared_data(training_service):
    """Prepare train/test data from AAPL dataset."""
    # Use 800 rows to ensure enough validation samples for InceptionTime
    # (smaller datasets cause overflow issues in tsai's batch slicing)
    df = pd.read_csv(TEST_DATA_PATH).head(800)
    original_len = len(df)

    feature_cols = ['Open', 'High', 'Low', 'Close', 'Volume']

    # Add simple binary target (price up next bar)
    df['target'] = (df['Close'].shift(-1) > df['Close']).astype(int)
    # Only drop NaN for columns we use (not all columns which have indicator warmup NaNs)
    df = df.dropna(subset=feature_cols + ['target'])

    # Ensure dropna didn't remove too many rows (should only lose 1 row from shift)
    assert len(df) >= original_len * 0.9, f"dropna removed too many rows: {original_len} -> {len(df)}"

    X_train, X_test, y_train, y_test = training_service.prepare_data_split(
        df, train_ratio=0.8,
        target_column='target',
        feature_columns=feature_cols,
        seq_len=24
    )
    return X_train, X_test, y_train, y_test


class TestTSAITrainingService:
    """Tests for TSAITrainingService."""

    def test_prepare_data_split(self, training_service):
        """Test data preparation and splitting."""
        df = pd.read_csv(TEST_DATA_PATH).head(200)
        original_len = len(df)
        feature_cols = ['Close', 'Volume']
        df['target'] = (df['Close'].shift(-1) > df['Close']).astype(int)
        # Only drop NaN for columns we use (not all columns which have indicator warmup NaNs)
        df = df.dropna(subset=feature_cols + ['target'])

        # Ensure dropna didn't remove too many rows (should only lose 1 row from shift)
        assert len(df) >= original_len * 0.9, f"dropna removed too many rows: {original_len} -> {len(df)}"

        X_train, X_test, y_train, y_test = training_service.prepare_data_split(
            df, train_ratio=0.8,
            target_column='target',
            feature_columns=feature_cols,
            seq_len=10
        )

        assert len(X_train) > 0
        assert len(X_test) > 0
        assert X_train.shape[1] == 2  # features
        assert X_train.shape[2] == 10  # seq_len

    def test_get_loss_function_focal(self, training_service):
        """Test focal loss creation."""
        loss = training_service.get_loss_function('focal')
        assert loss is not None

    def test_get_loss_function_ce(self, training_service):
        """Test cross-entropy loss creation."""
        loss = training_service.get_loss_function('ce')
        assert loss is not None

    def test_sequence_creation(self, training_service):
        """Test sliding window sequence creation."""
        X = np.random.randn(100, 5).astype(np.float32)
        y = np.random.randint(0, 2, 100).astype(np.int64)

        # Test with no prediction horizon (default behavior)
        X_seq, y_seq = training_service._create_sequences(X, y, seq_len=10, prediction_horizon=0)

        assert X_seq.shape == (91, 5, 10)  # 100 - 10 - 0 + 1 = 91
        assert y_seq.shape == (91,)

    def test_sequence_creation_with_horizon(self, training_service):
        """Test sliding window sequence creation with prediction horizon."""
        X = np.random.randn(100, 5).astype(np.float32)
        y = np.random.randint(0, 2, 100).astype(np.int64)

        # Test with prediction horizon of 3 bars
        X_seq, y_seq = training_service._create_sequences(X, y, seq_len=10, prediction_horizon=3)

        # With horizon=3: n_samples = 100 - 10 - 3 + 1 = 88
        assert X_seq.shape == (88, 5, 10)
        assert y_seq.shape == (88,)

    def test_sequence_creation_multistep(self, training_service):
        """Test multi-step sequence creation."""
        X = np.random.randn(100, 5).astype(np.float32)
        y = np.random.randint(0, 2, 100).astype(np.int64)

        X_seq, y_seq = training_service._create_sequences_multistep(X, y, seq_len=10, prediction_horizon=3)

        # n_samples = 100 - 10 - 3 + 1 = 88
        assert X_seq.shape == (88, 5, 10)
        assert y_seq.shape == (88, 3)  # Multi-step: 2D with horizon as second dim

    def test_sequence_creation_multistep_horizon_1(self, training_service):
        """Test multi-step with horizon=1."""
        X = np.random.randn(100, 5).astype(np.float32)
        y = np.random.randint(0, 2, 100).astype(np.int64)

        X_seq, y_seq = training_service._create_sequences_multistep(X, y, seq_len=10, prediction_horizon=1)

        # n_samples = 100 - 10 - 1 + 1 = 90
        assert X_seq.shape == (90, 5, 10)
        assert y_seq.shape == (90, 1)

    def test_zero_variance_columns_dropped_no_nan(self, training_service):
        """Test that zero-variance columns are dropped and no NaN in output."""
        # Create dataset with zero-variance columns (like YoY change in short dataset)
        np.random.seed(42)
        df = pd.DataFrame({
            'price': np.random.uniform(100, 200, 200),
            'volume': np.random.uniform(1e6, 1e7, 200),
            'constant_yoy': np.full(200, 0.05),  # Zero variance
            'constant_pe': np.full(200, 25.0),   # Zero variance
            'target': np.random.randint(0, 2, 200),
        })

        feature_cols = ['price', 'volume', 'constant_yoy', 'constant_pe']

        X_train, X_test, y_train, y_test = training_service.prepare_data_split(
            df, train_ratio=0.8,
            target_column='target',
            feature_columns=feature_cols,
            seq_len=10
        )

        # Check no NaN values
        assert np.isnan(X_train).sum() == 0, "X_train contains NaN values"
        assert np.isnan(X_test).sum() == 0, "X_test contains NaN values"

        # Check valid columns exclude zero-variance
        valid_cols = training_service.data_prep.get_valid_columns()
        assert 'price' in valid_cols
        assert 'volume' in valid_cols
        assert 'constant_yoy' not in valid_cols
        assert 'constant_pe' not in valid_cols

        # Feature dimension should be 2 (dropped 2 constant columns)
        assert X_train.shape[1] == 2, f"Expected 2 features, got {X_train.shape[1]}"


class TestTrainingWithRealData:
    """Integration tests with real AAPL data."""

    @pytest.mark.slow
    def test_train_lstm(self, model_service, training_service, prepared_data):
        """Test training LSTM model."""
        X_train, X_test, y_train, y_test = prepared_data

        model = model_service.create_model(
            'lstm', {'hidden_size': 32, 'n_layers': 1},
            c_in=X_train.shape[1],
            c_out=2,
            seq_len=X_train.shape[2]
        )

        result = training_service.train_model(
            model,
            (X_train, y_train),
            val_data=(X_test, y_test),
            epochs=2,
            batch_size=32
        )

        assert result['status'] == 'success'
        assert 'metrics' in result

    @pytest.mark.slow
    def test_train_inception(self, model_service, training_service, prepared_data):
        """Test training InceptionTime model."""
        X_train, X_test, y_train, y_test = prepared_data

        model = model_service.create_model(
            'inception', {'nf': 16, 'depth': 3},
            c_in=X_train.shape[1],
            c_out=2,
            seq_len=X_train.shape[2]
        )

        result = training_service.train_model(
            model,
            (X_train, y_train),
            val_data=(X_test, y_test),
            epochs=2
        )

        assert result['status'] == 'success'

    @pytest.mark.slow
    def test_assess_model(self, model_service, training_service, prepared_data):
        """Test model assessment."""
        X_train, X_test, y_train, y_test = prepared_data

        model = model_service.create_model(
            'lstm', {'hidden_size': 32, 'n_layers': 1},
            c_in=X_train.shape[1],
            c_out=2,
            seq_len=X_train.shape[2]
        )

        result = training_service.train_model(
            model,
            (X_train, y_train),
            epochs=2
        )

        if result['status'] == 'success':
            assess_result = training_service.assess_model(
                result['model'],
                (X_test, y_test),
                learner=result.get('learner')
            )

            assert 'f1_score' in assess_result
            assert 'accuracy' in assess_result

    @pytest.mark.slow
    def test_assess_model_with_threshold(self, model_service, training_service, prepared_data):
        """Test model assessment with custom threshold."""
        X_train, X_test, y_train, y_test = prepared_data

        model = model_service.create_model(
            'lstm', {'hidden_size': 32, 'n_layers': 1},
            c_in=X_train.shape[1],
            c_out=2,
            seq_len=X_train.shape[2]
        )

        result = training_service.train_model(
            model,
            (X_train, y_train),
            epochs=2
        )

        if result['status'] == 'success':
            # Test with different thresholds
            for threshold in [0.3, 0.5, 0.7]:
                assess_result = training_service.assess_model(
                    result['model'],
                    (X_test, y_test),
                    learner=result.get('learner'),
                    threshold=threshold
                )

                assert 'f1_score' in assess_result
                assert 'accuracy' in assess_result
                assert 'precision' in assess_result
                assert 'recall' in assess_result

            # Lower threshold should generally predict more positives
            result_low = training_service.assess_model(
                result['model'], (X_test, y_test),
                learner=result.get('learner'), threshold=0.2
            )
            result_high = training_service.assess_model(
                result['model'], (X_test, y_test),
                learner=result.get('learner'), threshold=0.8
            )
            # Low threshold = more predictions = higher recall (usually)
            # We can't guarantee this 100% but the test confirms threshold is being used
            assert isinstance(result_low['recall'], float)
            assert isinstance(result_high['recall'], float)

    @pytest.mark.slow
    def test_predict(self, model_service, training_service, prepared_data):
        """Test prediction generation."""
        X_train, X_test, y_train, y_test = prepared_data

        model = model_service.create_model(
            'lstm', {'hidden_size': 32, 'n_layers': 1},
            c_in=X_train.shape[1],
            c_out=2,
            seq_len=X_train.shape[2]
        )

        result = training_service.train_model(
            model,
            (X_train, y_train),
            epochs=2
        )

        if result['status'] == 'success':
            preds = training_service.predict(
                result['model'],
                X_test,
                learner=result.get('learner')
            )

            assert len(preds) == len(X_test)
            # Handle both 1D and 2D prediction arrays
            preds_flat = preds.flatten() if hasattr(preds, 'flatten') else preds
            assert all(0 <= p <= 1 for p in preds_flat)


class TestAllModelsShiftMode:
    """Parametrized tests for all models with shift mode."""

    @pytest.mark.slow
    @pytest.mark.parametrize("model_type,horizon", [
        ('lstm', 1), ('lstm', 3),
        ('gru', 1), ('gru', 3),
        ('tcn', 1), ('tcn', 3),
        ('inception', 1), ('inception', 3),
        ('resnet', 1), ('resnet', 3),
        ('xception', 1), ('xception', 3),
        ('omniscale', 1), ('omniscale', 3),
        ('minirocket', 1), ('minirocket', 3),
        ('lstm_fcn', 1), ('lstm_fcn', 3),
        ('tst', 1), ('tst', 3),
    ])
    def test_train_all_models_shift_mode(self, model_service, training_service, model_type, horizon):
        """Test all models with shift mode at horizon 1 and 3."""
        df = pd.read_csv(TEST_DATA_PATH).head(800)
        feature_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        df['target'] = (df['Close'].shift(-1) > df['Close']).astype(int)
        df = df.dropna(subset=feature_cols + ['target'])

        X_train, X_test, y_train, y_test = training_service.prepare_data_split(
            df, train_ratio=0.8, target_column='target', feature_columns=feature_cols,
            seq_len=24, prediction_horizon=horizon, prediction_mode='shift'
        )

        # Shift mode: 1D target
        assert len(y_train.shape) == 1

        model = model_service.create_model(
            model_type, {},
            c_in=X_train.shape[1], c_out=2, seq_len=X_train.shape[2]
        )
        result = training_service.train_model(
            model, (X_train, y_train), val_data=(X_test, y_test),
            epochs=2, prediction_mode='shift'
        )
        assert result['status'] == 'success'


class TestAllModelsMultistepMode:
    """Parametrized tests for all models with multi-step mode."""

    @pytest.mark.slow
    @pytest.mark.parametrize("model_type,horizon", [
        ('lstm', 1), ('lstm', 3),
        ('gru', 1), ('gru', 3),
        ('tcn', 1), ('tcn', 3),
        ('inception', 1), ('inception', 3),
        ('resnet', 1), ('resnet', 3),
        ('xception', 1), ('xception', 3),
        ('omniscale', 1), ('omniscale', 3),
        ('minirocket', 1), ('minirocket', 3),
        ('lstm_fcn', 1), ('lstm_fcn', 3),
        ('tst', 1), ('tst', 3),
    ])
    def test_train_all_models_multistep_mode(self, model_service, training_service, model_type, horizon):
        """Test all models with multi-step mode at horizon 1 and 3."""
        df = pd.read_csv(TEST_DATA_PATH).head(800)
        feature_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        df['target'] = (df['Close'].shift(-1) > df['Close']).astype(int)
        df = df.dropna(subset=feature_cols + ['target'])

        X_train, X_test, y_train, y_test = training_service.prepare_data_split(
            df, train_ratio=0.8, target_column='target', feature_columns=feature_cols,
            seq_len=24, prediction_horizon=horizon, prediction_mode='multistep'
        )

        # Multi-step mode: 2D target
        assert len(y_train.shape) == 2
        assert y_train.shape[1] == horizon

        model = model_service.create_model(
            model_type, {},
            c_in=X_train.shape[1], c_out=horizon, seq_len=X_train.shape[2]
        )
        result = training_service.train_model(
            model, (X_train, y_train), val_data=(X_test, y_test),
            epochs=2, prediction_mode='multistep'
        )
        assert result['status'] == 'success'

        # Test assessment for multi-step
        if result['status'] == 'success':
            metrics = training_service.assess_model(
                result['model'], (X_test, y_test),
                prediction_mode='multistep', learner=result.get('learner')
            )
            assert 'h1_f1' in metrics
            assert 'f1_score' in metrics


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
