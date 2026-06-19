"""
Tests for DataPreparationService - normalization and zero-variance handling.
"""

import pytest
import pandas as pd
import numpy as np
from app.services.data_preparation import DataPreparationService


class TestDataPreparationService:
    """Tests for DataPreparationService."""

    def test_minmax_buffered_normalization(self):
        """Test that minmax_buffered normalizes values to expected range."""
        df = pd.DataFrame({
            'feature1': [10.0, 20.0, 30.0, 40.0, 50.0],
            'feature2': [-5.0, 0.0, 5.0, 10.0, 15.0],
        })

        prep = DataPreparationService(buffer_pct=0.35)
        result = prep.fit_transform(df, ['feature1', 'feature2'], method='minmax_buffered')

        # With 35% buffer, values should be in range ~0.2 to ~0.8
        assert result['feature1'].min() >= 0.0
        assert result['feature1'].max() <= 1.0
        assert result['feature2'].min() >= 0.0
        assert result['feature2'].max() <= 1.0

        # No NaN values
        assert result['feature1'].isna().sum() == 0
        assert result['feature2'].isna().sum() == 0

    def test_zero_variance_column_dropped(self):
        """Test that zero-variance columns are dropped from valid columns."""
        df = pd.DataFrame({
            'constant': [5.0, 5.0, 5.0, 5.0, 5.0],  # Zero variance
            'varying': [1.0, 2.0, 3.0, 4.0, 5.0],   # Has variance
        })

        prep = DataPreparationService(buffer_pct=0.35)
        result = prep.fit_transform(df, ['constant', 'varying'], method='minmax_buffered')

        # Constant column should be dropped
        assert 'constant' in prep.dropped_columns
        assert 'constant' not in prep.valid_columns

        # Varying column should be valid
        assert 'varying' in prep.valid_columns
        assert 'varying' not in prep.dropped_columns

        # Valid columns count
        assert len(prep.valid_columns) == 1
        assert len(prep.dropped_columns) == 1

    def test_transform_no_nan_after_fit(self):
        """Test that transform produces no NaN values for zero-variance columns."""
        df = pd.DataFrame({
            'constant': [5.0, 5.0, 5.0, 5.0, 5.0],
            'varying': [1.0, 2.0, 3.0, 4.0, 5.0],
        })

        prep = DataPreparationService(buffer_pct=0.35)
        _ = prep.fit_transform(df, ['constant', 'varying'], method='minmax_buffered')

        # Transform a subset (simulating train/test split)
        df_subset = df.iloc[:3].copy()
        result = prep.transform(df_subset)

        # No NaN values in any column
        assert result['constant'].isna().sum() == 0
        assert result['varying'].isna().sum() == 0

    def test_transform_skips_dropped_columns(self):
        """Test that transform skips normalization for dropped columns."""
        df = pd.DataFrame({
            'constant': [5.0, 5.0, 5.0, 5.0, 5.0],
            'varying': [1.0, 2.0, 3.0, 4.0, 5.0],
        })

        prep = DataPreparationService(buffer_pct=0.35)
        _ = prep.fit_transform(df, ['constant', 'varying'], method='minmax_buffered')

        # Check params
        assert prep.normalization_params['constant'].get('dropped') is True
        assert prep.normalization_params['varying'].get('dropped') is None

    def test_negative_to_positive_range_handled(self):
        """Test that columns with negative to positive range are normalized correctly."""
        df = pd.DataFrame({
            'mixed': [-0.5, -0.2, 0.0, 0.3, 0.8],  # Negative to positive
        })

        prep = DataPreparationService(buffer_pct=0.35)
        result = prep.fit_transform(df, ['mixed'], method='minmax_buffered')

        # Should be normalized without NaN
        assert result['mixed'].isna().sum() == 0
        assert result['mixed'].min() >= 0.0
        assert result['mixed'].max() <= 1.0

    def test_export_params_includes_column_tracking(self):
        """Test that export_params includes valid and dropped columns."""
        df = pd.DataFrame({
            'constant': [5.0, 5.0, 5.0, 5.0, 5.0],
            'varying': [1.0, 2.0, 3.0, 4.0, 5.0],
        })

        prep = DataPreparationService(buffer_pct=0.35)
        _ = prep.fit_transform(df, ['constant', 'varying'], method='minmax_buffered')

        params = prep.export_params()

        assert 'valid_columns' in params
        assert 'dropped_columns' in params
        assert params['valid_columns'] == ['varying']
        assert params['dropped_columns'] == ['constant']
        assert params['version'] == '1.1'

    def test_load_params_restores_column_tracking(self):
        """Test that load_params correctly restores valid and dropped columns."""
        params = {
            'version': '1.1',
            'buffer_pct': 0.35,
            'columns': {},
            'valid_columns': ['feature1', 'feature2'],
            'dropped_columns': ['constant'],
        }

        prep = DataPreparationService()
        prep.load_params(params)

        assert prep.valid_columns == ['feature1', 'feature2']
        assert prep.dropped_columns == ['constant']

    def test_get_valid_columns_returns_copy(self):
        """Test that get_valid_columns returns a copy, not reference."""
        df = pd.DataFrame({
            'feature1': [1.0, 2.0, 3.0, 4.0, 5.0],
        })

        prep = DataPreparationService(buffer_pct=0.35)
        _ = prep.fit_transform(df, ['feature1'], method='minmax_buffered')

        valid = prep.get_valid_columns()
        valid.append('test')

        # Original should not be modified
        assert 'test' not in prep.valid_columns

    def test_real_world_scenario_no_nan(self):
        """Test with realistic financial data pattern."""
        np.random.seed(42)

        df = pd.DataFrame({
            # Varying features
            'price': np.random.uniform(100, 200, 100),
            'volume': np.random.uniform(1e6, 1e7, 100),
            'rsi': np.random.uniform(20, 80, 100),
            # Zero-variance features (like YoY change in short dataset)
            'yoy_change': np.full(100, 0.05),
            'constant_pe': np.full(100, 25.0),
            # Large values
            'market_cap': np.full(100, 3.8e12),
        })

        prep = DataPreparationService(buffer_pct=0.35)
        result = prep.fit_transform(
            df,
            ['price', 'volume', 'rsi', 'yoy_change', 'constant_pe', 'market_cap'],
            method='minmax_buffered'
        )

        # 3 zero-variance columns should be dropped
        assert len(prep.dropped_columns) == 3
        assert 'yoy_change' in prep.dropped_columns
        assert 'constant_pe' in prep.dropped_columns
        assert 'market_cap' in prep.dropped_columns

        # 3 valid columns
        assert len(prep.valid_columns) == 3

        # Transform on subset should produce no NaN
        df_subset = df.iloc[:50].copy()
        result_subset = prep.transform(df_subset)

        for col in df.columns:
            assert result_subset[col].isna().sum() == 0, f"NaN found in {col}"
