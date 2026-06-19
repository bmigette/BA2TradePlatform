#!/usr/bin/env python3
"""
Test the complete normalization flow:
1. Create train/test sets from datasets
2. Train a model with normalization
3. Save normalization params
4. Load normalization params and predict on test data
5. Verify predictions are consistent

Usage:
    cd backend
    ./venv/bin/python -m pytest tests/test_normalization_flow.py -v

Or run directly:
    ./venv/bin/python tests/test_normalization_flow.py
"""

import sys
import json
import tempfile
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.services.data_preparation import DataPreparationService


def _check_tsai_available() -> bool:
    """Check if tsai is available."""
    try:
        import torch
        from tsai.all import TSClassifier
        return True
    except ImportError:
        return False


class TestDataPreparationService:
    """Test DataPreparationService normalization and param export/import."""

    def create_sample_dataset(self, n_samples: int = 500) -> pd.DataFrame:
        """Create a realistic sample dataset for testing."""
        np.random.seed(42)

        # Generate dates
        dates = pd.date_range(start='2023-01-01', periods=n_samples, freq='1h')

        # Generate realistic OHLCV data
        base_price = 100.0
        returns = np.random.normal(0, 0.02, n_samples)
        close_prices = base_price * np.exp(np.cumsum(returns))

        # Add some variance between OHLC
        high_prices = close_prices * (1 + np.abs(np.random.normal(0, 0.005, n_samples)))
        low_prices = close_prices * (1 - np.abs(np.random.normal(0, 0.005, n_samples)))
        open_prices = close_prices * (1 + np.random.normal(0, 0.003, n_samples))

        # Volume with large range (like real data)
        volume = np.random.lognormal(mean=15, sigma=1.5, size=n_samples)

        # Technical indicators with different scales
        rsi = 30 + 40 * np.random.random(n_samples)  # 30-70 range
        macd = np.random.normal(0, 2, n_samples)  # Around 0
        atr = np.abs(high_prices - low_prices).mean() * (1 + np.random.normal(0, 0.3, n_samples))

        # Create a zero-variance column (should be dropped)
        constant_col = np.ones(n_samples) * 42.0

        # Binary target
        target = (np.random.random(n_samples) > 0.5).astype(int)

        df = pd.DataFrame({
            'Date': dates,
            'Open': open_prices,
            'High': high_prices,
            'Low': low_prices,
            'Close': close_prices,
            'Volume': volume,
            'RSI': rsi,
            'MACD': macd,
            'ATR': atr,
            'ConstantCol': constant_col,
            'target_directional': target
        })

        return df

    def test_fit_transform_and_export(self):
        """Test fitting normalization and exporting params."""
        df = self.create_sample_dataset(200)

        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume', 'RSI', 'MACD', 'ATR', 'ConstantCol']

        # Fit and transform
        prep = DataPreparationService(buffer_pct=0.35)
        df_normalized = prep.fit_transform(df, feature_columns)

        # Check that normalization params were created
        assert len(prep.normalization_params) > 0, "Normalization params should be created"

        # Check that zero-variance column was dropped
        assert 'ConstantCol' in prep.dropped_columns, "ConstantCol should be in dropped_columns"
        assert 'ConstantCol' not in prep.valid_columns, "ConstantCol should not be in valid_columns"

        # Check that other columns are valid
        expected_valid = ['Open', 'High', 'Low', 'Close', 'Volume', 'RSI', 'MACD', 'ATR']
        for col in expected_valid:
            assert col in prep.valid_columns, f"{col} should be in valid_columns"

        # Check normalized values are in expected range (0-1 with some tolerance for buffer)
        for col in prep.valid_columns:
            min_val = df_normalized[col].min()
            max_val = df_normalized[col].max()
            # With 35% buffer, values should be well within 0-1
            assert min_val >= 0, f"{col} min should be >= 0, got {min_val}"
            assert max_val <= 1, f"{col} max should be <= 1, got {max_val}"

        # Export params
        params = prep.export_params()
        assert 'version' in params
        assert 'buffer_pct' in params
        assert 'columns' in params
        assert 'valid_columns' in params
        assert 'dropped_columns' in params

        print(f"✓ Exported params with {len(params['valid_columns'])} valid columns, "
              f"{len(params['dropped_columns'])} dropped columns")

    def test_transform_with_loaded_params(self):
        """Test loading params and applying to new data."""
        df = self.create_sample_dataset(300)

        # Split into train/test (70/30)
        split_idx = int(len(df) * 0.7)
        df_train = df.iloc[:split_idx].copy()
        df_test = df.iloc[split_idx:].copy()

        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume', 'RSI', 'MACD', 'ATR']

        # Fit on training data
        prep_train = DataPreparationService(buffer_pct=0.35)
        df_train_norm = prep_train.fit_transform(df_train, feature_columns)

        # Export params
        params = prep_train.export_params()

        # Create new service and load params
        prep_test = DataPreparationService()
        prep_test.load_params(params)

        # Transform test data with loaded params
        df_test_norm = prep_test.transform(df_test)

        # Check that test data was normalized
        for col in feature_columns:
            assert col in df_test_norm.columns, f"{col} should be in test normalized data"
            # Test data might exceed training range slightly, but should be clipped
            min_val = df_test_norm[col].min()
            max_val = df_test_norm[col].max()
            assert min_val >= 0, f"{col} min should be >= 0 after clipping"
            assert max_val <= 1, f"{col} max should be <= 1 after clipping"

        print(f"✓ Test data normalized: shape {df_test_norm.shape}")

    def test_save_and_load_params_file(self):
        """Test saving params to file and loading them back."""
        df = self.create_sample_dataset(200)
        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume', 'RSI', 'MACD', 'ATR']

        # Fit and export
        prep = DataPreparationService(buffer_pct=0.35)
        prep.fit_transform(df, feature_columns)
        params = prep.export_params()

        # Save to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(params, f, indent=2)
            temp_path = f.name

        try:
            # Load from file
            prep2 = DataPreparationService()
            prep2.load_params_from_file(temp_path)

            # Check params were loaded correctly
            assert prep2.buffer_pct == prep.buffer_pct
            assert prep2.valid_columns == prep.valid_columns
            assert prep2.dropped_columns == prep.dropped_columns
            assert len(prep2.normalization_params) == len(prep.normalization_params)

            # Transform same data should give same results
            df_norm1 = prep.transform(df)
            df_norm2 = prep2.transform(df)

            for col in feature_columns:
                np.testing.assert_array_almost_equal(
                    df_norm1[col].values,
                    df_norm2[col].values,
                    decimal=10,
                    err_msg=f"Column {col} should be identical after loading params"
                )

            print(f"✓ Params saved and loaded correctly from {temp_path}")
        finally:
            Path(temp_path).unlink()


class TestTSAITrainingWithNormalization:
    """Test TSAITrainingService with normalization params."""

    def create_sample_dataset(self, n_samples: int = 500) -> pd.DataFrame:
        """Create a sample dataset for training."""
        np.random.seed(42)

        dates = pd.date_range(start='2023-01-01', periods=n_samples, freq='1h')

        # Generate price data
        base_price = 100.0
        returns = np.random.normal(0, 0.02, n_samples)
        close_prices = base_price * np.exp(np.cumsum(returns))

        high_prices = close_prices * (1 + np.abs(np.random.normal(0, 0.005, n_samples)))
        low_prices = close_prices * (1 - np.abs(np.random.normal(0, 0.005, n_samples)))
        open_prices = close_prices * (1 + np.random.normal(0, 0.003, n_samples))
        volume = np.random.lognormal(mean=15, sigma=1.5, size=n_samples)

        # Simple binary target based on next-bar direction
        target = np.zeros(n_samples, dtype=int)
        target[:-1] = (close_prices[1:] > close_prices[:-1]).astype(int)

        df = pd.DataFrame({
            'Date': dates,
            'Open': open_prices,
            'High': high_prices,
            'Low': low_prices,
            'Close': close_prices,
            'Volume': volume,
            'target_directional': target
        })

        return df

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_training_saves_normalization_params(self):
        """Test that training service saves normalization params."""
        from app.services.tsai_training import TSAITrainingService
        from app.services.tsai_models import TSAIModelService

        df = self.create_sample_dataset(300)
        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume']
        target_column = 'target_directional'

        # Create training service with normalization
        training_service = TSAITrainingService(normalize=True, buffer_pct=0.35)

        # Prepare data (this fits the scaler)
        X_train, X_test, y_train, y_test = training_service.prepare_data_split(
            df=df,
            train_ratio=0.7,
            target_column=target_column,
            feature_columns=feature_columns,
            seq_len=24,
            prediction_horizon=0,
            prediction_mode='shift'
        )

        # Check that normalization params are available
        norm_params = training_service.get_normalization_params()
        assert norm_params is not None, "Normalization params should be available"
        assert 'columns' in norm_params, "Params should have columns"
        assert len(norm_params['valid_columns']) > 0, "Should have valid columns"

        print(f"✓ Training service has normalization params: "
              f"{len(norm_params['valid_columns'])} valid columns")

        # Create and train a simple model
        model_service = TSAIModelService()
        model = model_service.create_model(
            model_type='lstm',
            params={'hidden_size': 32, 'n_layers': 1, 'dropout': 0.1},
            c_in=X_train.shape[1],
            c_out=2,
            seq_len=X_train.shape[2]
        )

        # Train for just 1 epoch (testing flow, not model quality)
        result = training_service.train_model(
            model=model,
            train_data=(X_train, y_train),
            val_data=(X_test, y_test),
            epochs=1,
            batch_size=32,
            learning_rate=0.001
        )

        assert result['status'] == 'success', f"Training should succeed: {result.get('error')}"

        # Verify normalization params are still available
        norm_params_after = training_service.get_normalization_params()
        assert norm_params_after is not None
        # Compare key fields (ignore timestamp which changes)
        assert norm_params_after['columns'] == norm_params['columns']
        assert norm_params_after['valid_columns'] == norm_params['valid_columns']

        print("✓ Training completed with normalization params preserved")

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_prediction_with_saved_normalization_params(self):
        """Test that predictions work correctly with saved normalization params."""
        from app.services.tsai_training import TSAITrainingService
        from app.services.tsai_models import TSAIModelService

        df = self.create_sample_dataset(400)
        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume']
        target_column = 'target_directional'

        # Split data: 70% train, 15% validation, 15% test (simulating forward test)
        n = len(df)
        train_end = int(n * 0.7)
        val_end = int(n * 0.85)

        df_train = df.iloc[:train_end].copy()
        df_val = df.iloc[train_end:val_end].copy()
        df_test = df.iloc[val_end:].copy()  # "Future" data for forward test

        # === TRAINING PHASE ===
        # Create training service and prepare data
        training_service = TSAITrainingService(normalize=True, buffer_pct=0.35)

        # Fit normalization on training data only
        X_train, y_train = training_service.prepare_data(
            df=df_train,
            target_column=target_column,
            feature_columns=feature_columns,
            seq_len=24,
            prediction_horizon=0,
            prediction_mode='shift',
            fit_scaler=True  # Fit on training data
        )

        # Prepare validation data with same scaler
        X_val, y_val = training_service.prepare_data(
            df=df_val,
            target_column=target_column,
            feature_columns=feature_columns,
            seq_len=24,
            prediction_horizon=0,
            prediction_mode='shift',
            fit_scaler=False  # Use already-fitted scaler
        )

        # Export normalization params (simulate saving to DB/file)
        norm_params = training_service.get_normalization_params()
        norm_params_json = json.dumps(norm_params)  # Simulate serialization

        # Train model
        model_service = TSAIModelService()
        model = model_service.create_model(
            model_type='lstm',
            params={'hidden_size': 32, 'n_layers': 1, 'dropout': 0.1},
            c_in=X_train.shape[1],
            c_out=2,
            seq_len=X_train.shape[2]
        )

        result = training_service.train_model(
            model=model,
            train_data=(X_train, y_train),
            val_data=(X_val, y_val),
            epochs=2,
            batch_size=32,
            learning_rate=0.001
        )

        assert result['status'] == 'success'
        trained_model = result['model']

        # Get predictions on validation data during training
        val_probs_training = training_service.predict(trained_model, X_val)

        # === FORWARD TEST PHASE (simulate loading model for inference) ===
        # Create new training service (simulating fresh inference context)
        inference_service = TSAITrainingService(normalize=True)

        # Load normalization params (simulate loading from DB)
        loaded_params = json.loads(norm_params_json)
        inference_service.data_prep = DataPreparationService()
        inference_service.data_prep.load_params(loaded_params)

        # Prepare test data with loaded normalization params
        X_test, y_test = inference_service.prepare_data(
            df=df_test,
            target_column=target_column,
            feature_columns=feature_columns,
            seq_len=24,
            prediction_horizon=0,
            prediction_mode='shift',
            fit_scaler=False  # Use loaded scaler, don't refit
        )

        # Run predictions on test data
        test_probs = inference_service.predict(trained_model, X_test)

        # Verify predictions are valid (not NaN, in valid range)
        assert not np.isnan(test_probs).any(), "Predictions should not contain NaN"
        assert test_probs.min() >= 0, "Probabilities should be >= 0"
        assert test_probs.max() <= 1, "Probabilities should be <= 1"

        # Check that predictions have expected shape
        assert test_probs.shape[0] == len(X_test), "Should have prediction for each sample"
        assert test_probs.shape[1] == 2, "Should have 2 classes (binary classification)"

        print(f"✓ Forward test predictions: shape {test_probs.shape}, "
              f"mean prob: {test_probs[:, 1].mean():.4f}")

        # === CONSISTENCY CHECK ===
        # Re-prepare validation data with the loaded params and verify same results
        X_val_reload, _ = inference_service.prepare_data(
            df=df_val,
            target_column=target_column,
            feature_columns=feature_columns,
            seq_len=24,
            prediction_horizon=0,
            prediction_mode='shift',
            fit_scaler=False
        )

        val_probs_reload = inference_service.predict(trained_model, X_val_reload)

        # Predictions should be identical (same model, same data, same normalization)
        np.testing.assert_array_almost_equal(
            val_probs_training,
            val_probs_reload,
            decimal=5,
            err_msg="Predictions should be identical with loaded normalization params"
        )

        print("✓ Predictions are consistent when using saved normalization params")


class TestEndToEndNormalizationFlow:
    """End-to-end test simulating the full workflow."""

    def test_full_workflow_with_metadata_file(self):
        """Test the complete workflow: train -> save metadata -> load -> predict."""
        if not _check_tsai_available():
            pytest.skip("tsai not available")

        from app.services.tsai_training import TSAITrainingService
        from app.services.tsai_models import TSAIModelService
        import torch

        # Create sample dataset
        np.random.seed(42)
        n_samples = 400
        dates = pd.date_range(start='2023-01-01', periods=n_samples, freq='1h')

        base_price = 100.0
        returns = np.random.normal(0, 0.02, n_samples)
        close_prices = base_price * np.exp(np.cumsum(returns))

        df = pd.DataFrame({
            'Date': dates,
            'Open': close_prices * (1 + np.random.normal(0, 0.003, n_samples)),
            'High': close_prices * (1 + np.abs(np.random.normal(0, 0.005, n_samples))),
            'Low': close_prices * (1 - np.abs(np.random.normal(0, 0.005, n_samples))),
            'Close': close_prices,
            'Volume': np.random.lognormal(mean=15, sigma=1.5, size=n_samples),
            'target_directional': (np.random.random(n_samples) > 0.5).astype(int)
        })

        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume']
        target_column = 'target_directional'
        seq_len = 24

        # Split data
        split_idx = int(len(df) * 0.8)
        df_train = df.iloc[:split_idx]
        df_test = df.iloc[split_idx:]

        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = Path(tmpdir) / "test_model.pt"
            meta_path = Path(tmpdir) / "test_model_meta.json"

            # === TRAINING ===
            training_service = TSAITrainingService(normalize=True, buffer_pct=0.35)

            X_train, X_test, y_train, y_test = training_service.prepare_data_split(
                df=df_train,
                train_ratio=0.8,
                target_column=target_column,
                feature_columns=feature_columns,
                seq_len=seq_len
            )

            # Get normalization params
            norm_params = training_service.get_normalization_params()
            valid_feature_columns = training_service.data_prep.get_valid_columns()

            # Create and train model
            model_service = TSAIModelService()
            c_in = X_train.shape[1]
            c_out = 2

            model = model_service.create_model(
                model_type='lstm',
                params={'hidden_size': 32, 'n_layers': 1, 'dropout': 0.1},
                c_in=c_in,
                c_out=c_out,
                seq_len=seq_len
            )

            result = training_service.train_model(
                model=model,
                train_data=(X_train, y_train),
                val_data=(X_test, y_test),
                epochs=2,
                batch_size=32
            )

            assert result['status'] == 'success'
            trained_model = result['model']

            # Save model weights
            torch.save(trained_model.state_dict(), model_path)

            # Save metadata (simulating what job_handler does)
            metadata = {
                'model_type': 'lstm',
                'c_in': c_in,
                'c_out': c_out,
                'seq_len': seq_len,
                'params': {'hidden_size': 32, 'n_layers': 1, 'dropout': 0.1},
                'feature_columns': valid_feature_columns,
                'normalization_params': norm_params
            }

            with open(meta_path, 'w') as f:
                json.dump(metadata, f, indent=2)

            print(f"✓ Saved model to {model_path}")
            print(f"✓ Saved metadata to {meta_path}")

            # === INFERENCE (simulate loading for forward test) ===
            # Load metadata
            with open(meta_path, 'r') as f:
                loaded_meta = json.load(f)

            # Verify normalization params were saved
            assert loaded_meta.get('normalization_params') is not None, \
                "Normalization params should be in metadata"

            # Create inference service with loaded normalization
            inference_service = TSAITrainingService(normalize=True)
            inference_service.data_prep = DataPreparationService()
            inference_service.data_prep.load_params(loaded_meta['normalization_params'])

            # Prepare test data (using saved feature columns)
            loaded_feature_cols = loaded_meta['feature_columns']
            X_forward, y_forward = inference_service.prepare_data(
                df=df_test,
                target_column=target_column,
                feature_columns=loaded_feature_cols,
                seq_len=loaded_meta['seq_len'],
                fit_scaler=False  # Use loaded scaler
            )

            # Recreate model architecture
            model_service2 = TSAIModelService()
            inference_model = model_service2.create_model(
                model_type=loaded_meta['model_type'],
                params=loaded_meta['params'],
                c_in=loaded_meta['c_in'],
                c_out=loaded_meta['c_out'],
                seq_len=loaded_meta['seq_len']
            )

            # Load weights
            state_dict = torch.load(model_path, map_location='cpu', weights_only=True)
            inference_model.load_state_dict(state_dict)

            # Run predictions
            probs = inference_service.predict(inference_model, X_forward)

            # Verify results
            assert not np.isnan(probs).any(), "Predictions should not contain NaN"
            assert probs.shape[0] == len(X_forward), "Should have prediction for each sample"
            assert probs.shape[1] == 2, "Should have 2 classes"

            # Calculate predictions
            predictions = (probs[:, 1] >= 0.5).astype(int)
            accuracy = (predictions == y_forward).mean()

            print(f"✓ Forward test completed:")
            print(f"  - Test samples: {len(X_forward)}")
            print(f"  - Predictions shape: {probs.shape}")
            print(f"  - Probability range: [{probs[:, 1].min():.4f}, {probs[:, 1].max():.4f}]")
            print(f"  - Mean probability: {probs[:, 1].mean():.4f}")
            print(f"  - Accuracy: {accuracy:.4f}")

            assert probs[:, 1].min() >= 0 and probs[:, 1].max() <= 1, \
                "Probabilities should be in [0, 1] range"


def run_tests():
    """Run all tests manually."""
    print("\n" + "="*60)
    print("TESTING NORMALIZATION FLOW")
    print("="*60 + "\n")

    # Test DataPreparationService
    print("\n--- DataPreparationService Tests ---\n")

    test_prep = TestDataPreparationService()

    print("Test 1: fit_transform and export")
    test_prep.test_fit_transform_and_export()

    print("\nTest 2: transform with loaded params")
    test_prep.test_transform_with_loaded_params()

    print("\nTest 3: save and load params file")
    test_prep.test_save_and_load_params_file()

    # Test with tsai if available
    if _check_tsai_available():
        print("\n--- TSAITrainingService Tests ---\n")

        test_tsai = TestTSAITrainingWithNormalization()

        print("Test 4: training saves normalization params")
        test_tsai.test_training_saves_normalization_params()

        print("\nTest 5: prediction with saved normalization params")
        test_tsai.test_prediction_with_saved_normalization_params()

        print("\n--- End-to-End Tests ---\n")

        test_e2e = TestEndToEndNormalizationFlow()

        print("Test 6: full workflow with metadata file")
        test_e2e.test_full_workflow_with_metadata_file()
    else:
        print("\n⚠ Skipping tsai tests (tsai not available)")

    print("\n" + "="*60)
    print("ALL TESTS PASSED ✓")
    print("="*60 + "\n")


if __name__ == "__main__":
    run_tests()
