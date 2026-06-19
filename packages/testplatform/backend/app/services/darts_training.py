"""
Darts Training Service

Provides model training, evaluation, and saving functionality.
Integrates with Darts library for timeseries model training.
This service is designed for REGRESSION tasks (time series forecasting).
"""

import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
import logging
import os
from pathlib import Path
import json

from app.services.model_interface import ITrainingService

logger = logging.getLogger(__name__)

# Check for required libraries
try:
    import torch
    TORCH_AVAILABLE = True
    # Check if MPS (Apple Silicon GPU) will be used - it doesn't support float64
    MPS_WILL_BE_USED = (
        hasattr(torch.backends, 'mps') and
        torch.backends.mps.is_available() and
        torch.backends.mps.is_built()
    )
except ImportError:
    TORCH_AVAILABLE = False
    MPS_WILL_BE_USED = False

try:
    from darts import TimeSeries, concatenate
    from darts.models import RNNModel, NBEATSModel
    from darts.dataprocessing.transformers import Scaler
    from darts.metrics import mape, mae, rmse
    DARTS_AVAILABLE = True
except ImportError:
    DARTS_AVAILABLE = False


class DartsTrainingService(ITrainingService):
    """
    Darts-based training service for time series regression/forecasting.
    """

    def __init__(self, models_dir: str = None):
        """
        Initialize TrainingService.

        Args:
            models_dir: Directory to save trained models. Defaults to the
                test-bucket models dir (app.paths.MODELS_DIR) — not the repo/CWD.
        """
        if models_dir is None:
            from app.paths import MODELS_DIR
            self.models_dir = Path(MODELS_DIR)
        else:
            self.models_dir = Path(models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.scaler = None

    def prepare_data(
        self,
        df: pd.DataFrame,
        target_column: str = 'Close',
        feature_columns: List[str] = None,
        timeframe: str = 'daily'
    ) -> Tuple[Any, Any]:
        """
        Prepare data for Darts model training.

        Args:
            df: DataFrame with Date and target columns
            target_column: Column to predict
            feature_columns: Optional covariate columns
            timeframe: Dataset timeframe for frequency inference

        Returns:
            Tuple of (target_series, covariates_series)
        """
        if not DARTS_AVAILABLE:
            raise RuntimeError("Darts library not available")

        df_sorted = df.sort_values('Date').copy()
        df_sorted['Date'] = pd.to_datetime(df_sorted['Date'])
        df_sorted = df_sorted.set_index('Date')

        # Infer frequency based on timeframe
        freq = self._infer_frequency(timeframe, df_sorted)

        # Drop rows with NaN in target column
        if target_column in df_sorted.columns:
            df_sorted = df_sorted.dropna(subset=[target_column])

        # For intraday stock data (1h, 4h, etc.), don't use fill_missing_dates
        # because market data has irregular timestamps (market hours, weekends, holidays)
        # that cannot be filled with a regular frequency grid
        is_intraday = timeframe.lower() in ['1m', '5m', '15m', '30m', '1h', '4h']

        if is_intraday:
            # For intraday stock data: use from_values() which treats data as
            # an ordered sequence without time semantics (ignores market hours gaps)
            # Ensure float type for compatibility with classification loss functions
            target_values = df_sorted[[target_column]].values.astype('float32')
            target_series = TimeSeries.from_values(target_values)
        else:
            # For daily/weekly: fill missing dates (weekends/holidays)
            # Ensure target column is float for compatibility with loss functions
            df_target = df_sorted[[target_column]].copy()
            df_target[target_column] = df_target[target_column].astype('float32')
            target_series = TimeSeries.from_dataframe(
                df_target,
                value_cols=target_column,
                fill_missing_dates=True,
                freq=freq
            )

        # Scale the data
        self.scaler = Scaler()
        target_series = self.scaler.fit_transform(target_series)

        # Always convert to float32 for compatibility with loss functions
        # (FocalLoss and other classification losses require float targets)
        target_series = target_series.astype('float32')

        # Create covariates if specified
        covariates = None
        if feature_columns:
            available_cols = [c for c in feature_columns if c in df_sorted.columns]
            if available_cols:
                # Use the same rows as target series to ensure alignment
                # Fill NaN with forward fill then backward fill instead of dropping rows
                cov_df = df_sorted[available_cols].ffill().bfill()

                # If still has NaN (e.g., all NaN column), fill with 0
                cov_df = cov_df.fillna(0)

                if len(cov_df) > 0 and len(cov_df) == len(df_sorted):
                    if is_intraday:
                        cov_values = cov_df[available_cols].values
                        covariates = TimeSeries.from_values(cov_values)
                    else:
                        covariates = TimeSeries.from_dataframe(
                            cov_df,
                            value_cols=available_cols,
                            fill_missing_dates=True,
                            freq=freq
                        )
                    # Scale covariates
                    cov_scaler = Scaler()
                    covariates = cov_scaler.fit_transform(covariates)

                    # MPS (Apple Silicon GPU) doesn't support float64
                    if MPS_WILL_BE_USED:
                        covariates = covariates.astype('float32')

                    logger.info(f"Created covariates with {len(covariates)} points from {len(available_cols)} features")
                else:
                    logger.warning(f"Covariate length mismatch: {len(cov_df)} vs target {len(df_sorted)}, skipping covariates")

        return target_series, covariates

    def prepare_data_split(
        self,
        df: pd.DataFrame,
        train_ratio: float = 0.8,
        target_column: str = 'Close',
        feature_columns: List[str] = None,
        timeframe: str = 'daily'
    ) -> Tuple[Any, Any, Any, Any]:
        """
        Prepare data and split into train/test TimeSeries with continuous indices.

        This ensures train and test series share the same index space, which is
        required for Darts metric functions (mape, mae, etc.) to work correctly.

        Args:
            df: Full DataFrame with Date and target columns
            train_ratio: Fraction of data for training (0.0 to 1.0)
            target_column: Column to predict
            feature_columns: Optional covariate columns
            timeframe: Dataset timeframe for frequency inference

        Returns:
            Tuple of (train_series, test_series, train_covariates, test_covariates)
        """
        # Prepare full data as one TimeSeries
        full_series, full_covariates = self.prepare_data(
            df, target_column, feature_columns, timeframe
        )

        # Calculate split point
        split_idx = int(len(full_series) * train_ratio)

        # Split series using slicing (preserves index continuity)
        train_series = full_series[:split_idx]
        test_series = full_series[split_idx:]

        # Split covariates if present
        train_covariates = None
        test_covariates = None
        if full_covariates is not None:
            train_covariates = full_covariates[:split_idx]
            test_covariates = full_covariates[split_idx:]

        logger.info(f"Split data: train={len(train_series)}, test={len(test_series)} "
                    f"(indices {split_idx} to {len(full_series)-1})")

        return train_series, test_series, train_covariates, test_covariates

    def prepare_multi_series(
        self, dataframes: List[pd.DataFrame], target_column: str = 'Close',
        feature_columns: List[str] = None, timeframe: str = 'daily'
    ) -> Tuple[List[Any], List[Any]]:
        """Prepare multiple DataFrames as separate TimeSeries for multi-series training."""
        all_series = []
        all_covariates = []
        for i, df in enumerate(dataframes):
            series, covariates = self.prepare_data(df, target_column, feature_columns, timeframe)
            all_series.append(series)
            all_covariates.append(covariates)
            logger.info(f"Prepared series {i+1}/{len(dataframes)}: {len(series)} points")
        return all_series, all_covariates

    def prepare_multi_series_split(
        self, dataframes: List[pd.DataFrame], train_ratio: float = 0.8,
        target_column: str = 'Close', feature_columns: List[str] = None,
        timeframe: str = 'daily'
    ) -> Tuple[List[Any], List[Any], List[Any], List[Any]]:
        """Prepare and split multiple DataFrames into train/test TimeSeries lists."""
        train_series_list = []
        test_series_list = []
        train_cov_list = []
        test_cov_list = []
        for i, df in enumerate(dataframes):
            train_s, test_s, train_c, test_c = self.prepare_data_split(
                df, train_ratio, target_column, feature_columns, timeframe
            )
            train_series_list.append(train_s)
            test_series_list.append(test_s)
            train_cov_list.append(train_c)
            test_cov_list.append(test_c)
        return train_series_list, test_series_list, train_cov_list, test_cov_list

    def _infer_frequency(self, timeframe: str, df: pd.DataFrame) -> str:
        """
        Infer pandas frequency string from timeframe.

        Args:
            timeframe: Dataset timeframe (e.g., 'daily', '1h', '4h', '1d')
            df: DataFrame with DatetimeIndex

        Returns:
            Pandas frequency string
        """
        timeframe_lower = timeframe.lower()

        # Map common timeframes to pandas frequencies
        # Note: Using lowercase 'h' for hours and 'min' for minutes (uppercase deprecated in pandas 2.2+)
        freq_map = {
            '1m': 'min',     # 1 minute
            '5m': '5min',    # 5 minutes
            '15m': '15min',  # 15 minutes
            '30m': '30min',  # 30 minutes
            '1h': 'h',       # 1 hour
            '4h': '4h',      # 4 hours
            '1d': 'D',       # 1 day
            'daily': 'D',    # daily
            '1w': 'W',       # 1 week
            'weekly': 'W',   # weekly
        }

        if timeframe_lower in freq_map:
            return freq_map[timeframe_lower]

        # Try to infer from data
        if len(df) >= 2:
            try:
                # Calculate median difference between consecutive rows
                time_diffs = df.index.to_series().diff().dropna()
                median_diff = time_diffs.median()

                if median_diff <= pd.Timedelta(minutes=5):
                    return '5min'
                elif median_diff <= pd.Timedelta(hours=1):
                    return 'h'
                elif median_diff <= pd.Timedelta(hours=4):
                    return '4h'
                elif median_diff <= pd.Timedelta(days=1):
                    return 'D'
                else:
                    return 'W'
            except Exception:
                pass

        # Default to daily
        logger.warning(f"Could not infer frequency for timeframe '{timeframe}', using daily")
        return 'D'

    def train_model(
        self,
        model: Any,
        train_series: Any,
        val_series: Any = None,
        covariates: Any = None,
        verbose: bool = True
    ) -> Dict[str, Any]:
        """
        Train a Darts model.

        Args:
            model: Darts model instance
            train_series: Training TimeSeries
            val_series: Optional validation TimeSeries
            covariates: Optional covariate TimeSeries
            verbose: Whether to print training progress

        Returns:
            Training metrics
        """
        if not DARTS_AVAILABLE:
            raise RuntimeError("Darts library not available")

        start_time = datetime.now()

        logger.info(f"Starting training with {len(train_series)} data points")

        try:
            # Check if multi-series mode (list of TimeSeries)
            is_multi = isinstance(train_series, list)

            if is_multi:
                # Multi-series training: model.fit(series=[ts1, ts2, ...])
                fit_kwargs = {'verbose': verbose}
                if covariates is not None and isinstance(covariates, list):
                    valid_covariates = [c for c in covariates if c is not None]
                    if len(valid_covariates) == len(train_series):
                        model_name = model.__class__.__name__
                        if model_name != 'RNNModel':
                            fit_kwargs['past_covariates'] = valid_covariates
                model.fit(train_series, **fit_kwargs)

                training_time = (datetime.now() - start_time).total_seconds()
                total_samples = sum(len(s) for s in train_series)
                metrics = {
                    'training_time_seconds': training_time,
                    'train_samples': total_samples,
                    'num_series': len(train_series),
                    'status': 'completed'
                }
                logger.info(f"Multi-series training completed in {training_time:.2f}s ({len(train_series)} series, {total_samples} total points)")
                return metrics

            # Single series training (existing code below)
            # Build fit kwargs with optional val_series for validation metrics during training
            fit_kwargs = {'verbose': verbose}

            if covariates is not None:
                # Check if model supports past_covariates
                # RNNModel (LSTM, GRU, RNN) only supports future_covariates, not past_covariates
                model_name = model.__class__.__name__
                if model_name == 'RNNModel':
                    # RNN-based models don't support past_covariates, this is expected
                    logger.info(f"{model_name} does not support past_covariates, training without covariates")
                    # Can use val_series for RNN models since no covariates needed
                    if val_series is not None:
                        fit_kwargs['val_series'] = val_series
                        logger.debug(f"Training with validation set ({len(val_series)} points)")
                    model.fit(train_series, **fit_kwargs)
                else:
                    # Train with covariates - skip val_series to avoid covariate alignment issues
                    # Validation covariates would need to cover the test range which complicates things
                    fit_kwargs['past_covariates'] = covariates
                    logger.debug(f"Training with covariates, skipping val_series to avoid alignment issues")
                    model.fit(train_series, **fit_kwargs)
            else:
                # No covariates - can safely use val_series
                if val_series is not None:
                    fit_kwargs['val_series'] = val_series
                    logger.debug(f"Training with validation set ({len(val_series)} points)")
                model.fit(train_series, **fit_kwargs)

            training_time = (datetime.now() - start_time).total_seconds()

            # Note: Callbacks are now serializable (EpochProgressCallback implements
            # __getstate__/__setstate__), so no need to clear them before saving.

            metrics = {
                'training_time_seconds': training_time,
                'train_samples': len(train_series),
                'status': 'completed'
            }

            # Note: Training metrics (train_mape, train_mae) are not calculated here
            # because model.predict() forecasts future values beyond the training data,
            # not within it. Use evaluate_model() with a proper test set for metrics.

            logger.info(f"Training completed in {training_time:.2f} seconds")
            return metrics

        except Exception as e:
            logger.error(f"Training failed: {e}")
            return {
                'status': 'failed',
                'error': str(e)
            }

    # Classification metrics that require thresholding predictions
    CLASSIFICATION_METRICS = {'f1_score', 'accuracy', 'precision', 'recall', 'auc_roc', 'balanced_accuracy', 'mcc', 'auc_pr'}

    def evaluate_model(
        self,
        model: Any,
        test_series: Any,
        covariates: Any = None,
        optimize_metric: str = 'mape',
        threshold: float = 0.5
    ) -> Dict[str, float]:
        """
        Evaluate model on test set.

        For classification metrics, uses historical_forecasts to make rolling predictions
        across the test set, ensuring enough samples for meaningful metrics.

        For regression metrics, uses single prediction for efficiency.

        Args:
            model: Trained Darts model
            test_series: Test TimeSeries
            covariates: Optional covariate TimeSeries
            optimize_metric: Metric to optimize ('f1_score', 'accuracy', 'mape', etc.)
            threshold: Classification threshold for binary metrics (default 0.5)

        Returns:
            Evaluation metrics
        """
        if not DARTS_AVAILABLE:
            raise RuntimeError("Darts library not available")

        try:
            is_classification = optimize_metric in self.CLASSIFICATION_METRICS

            if is_classification:
                # For classification, use historical_forecasts to get predictions
                # across the entire test set for meaningful metrics
                return self._evaluate_classification(
                    model, test_series, covariates, optimize_metric, threshold
                )
            else:
                # For regression, use simple prediction
                return self._evaluate_regression(
                    model, test_series, covariates, optimize_metric
                )

        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            import traceback
            traceback.print_exc()
            return {'error': str(e)}

    def _evaluate_classification(
        self,
        model: Any,
        test_series: Any,
        covariates: Any = None,
        optimize_metric: str = 'f1_score',
        threshold: float = 0.5
    ) -> Dict[str, float]:
        """
        Evaluate classification model using historical_forecasts.

        Uses stride=1 to evaluate ALL test samples, simulating real-time
        predictions where at each time T we predict T+1 using history up to T.
        """
        from app.services.metrics import ClassificationMetrics

        input_chunk = model.input_chunk_length
        output_chunk = model.output_chunk_length

        min_required = input_chunk + output_chunk
        if len(test_series) < min_required:
            logger.warning(f"Test series too short ({len(test_series)} < {min_required})")
            n_predict = min(output_chunk, len(test_series))
            if n_predict <= 0:
                return {'error': 'Test series too short'}
            predictions = model.predict(n=n_predict)
            actuals = test_series[:n_predict]
        else:
            try:
                # Use stride=1 to evaluate EVERY test sample
                stride = 1
                expected_predictions = len(test_series) - input_chunk

                logger.debug(f"Historical forecasts: test_len={len(test_series)}, "
                           f"input_chunk={input_chunk}, output_chunk={output_chunk}, "
                           f"stride={stride}, expected_predictions={expected_predictions}")

                # Build kwargs for historical_forecasts
                hf_kwargs = {
                    'series': test_series,
                    'start': input_chunk,  # Start after enough history
                    'forecast_horizon': 1,  # Predict 1 step at a time
                    'stride': stride,
                    'retrain': False,
                    'verbose': False,
                    'show_warnings': False,
                    'last_points_only': True
                }

                # Add covariates if model was trained with them
                model_uses_covariates = (
                    hasattr(model, 'past_covariate_series') and
                    model.past_covariate_series is not None
                )
                if model_uses_covariates and covariates is not None:
                    hf_kwargs['past_covariates'] = covariates
                    logger.debug("Using past_covariates for historical forecasts")

                # Make historical forecasts
                forecasts = model.historical_forecasts(**hf_kwargs)

                # Combine all forecasts
                if isinstance(forecasts, list):
                    if len(forecasts) == 0:
                        return {'error': 'No forecasts generated'}
                    predictions = concatenate(forecasts)
                else:
                    predictions = forecasts

                # Get actuals matching the prediction time indices
                actuals = test_series.slice_intersect(predictions)

                logger.debug(f"Historical forecasts: {len(predictions)} predictions, {len(actuals)} actuals")

            except Exception as e:
                logger.warning(f"Historical forecasts failed: {e}, falling back to simple prediction")
                n_predict = min(output_chunk, len(test_series))
                predictions = model.predict(n=n_predict)
                actuals = test_series[:n_predict]

        if len(predictions) == 0 or len(actuals) == 0:
            return {'error': 'No valid predictions could be made'}

        # Inverse-transform to original scale
        if self.scaler is not None:
            predictions_orig = self.scaler.inverse_transform(predictions)
            actuals_orig = self.scaler.inverse_transform(actuals)
        else:
            predictions_orig = predictions
            actuals_orig = actuals

        # Get numpy arrays
        pred_values = predictions_orig.values().flatten()
        actual_values = actuals_orig.values().flatten()

        # For classification: apply sigmoid to convert logits to probabilities
        # This is needed when using FocalLoss or other logit-based loss functions
        # Sigmoid: 1 / (1 + exp(-x))
        pred_proba = 1 / (1 + np.exp(-pred_values))

        # Ensure predictions are in valid probability range [0, 1]
        pred_proba = np.clip(pred_proba, 0, 1)

        # Actuals should be binary (0 or 1) - round to handle any float noise
        actual_binary = np.round(actual_values).astype(int)

        # Debug: log prediction and actual distributions
        pred_binary = (pred_proba >= threshold).astype(int)
        n_samples = len(pred_proba)
        n_pred_positive = pred_binary.sum()
        n_actual_positive = actual_binary.sum()
        logger.debug(f"Classification debug: n_samples={n_samples}, "
                   f"pred_proba range=[{pred_proba.min():.4f}, {pred_proba.max():.4f}], "
                   f"pred_positive={n_pred_positive}/{n_samples} ({100*n_pred_positive/n_samples:.1f}%), "
                   f"actual_positive={n_actual_positive}/{n_samples} ({100*n_actual_positive/n_samples:.1f}%)")

        # Calculate all classification metrics
        class_metrics = ClassificationMetrics.calculate_all(actual_binary, pred_proba, threshold)

        metrics = {
            'f1_score': class_metrics['f1_score'],
            'accuracy': class_metrics['accuracy'],
            'precision': class_metrics['precision'],
            'recall': class_metrics['recall'],
            'balanced_accuracy': class_metrics['balanced_accuracy'],
            'mcc': class_metrics['mcc'],
            'auc_roc': class_metrics.get('auc_roc', 0.0),
            'auc_pr': class_metrics.get('auc_pr', 0.0),
            'true_positives': class_metrics['true_positives'],
            'false_positives': class_metrics['false_positives'],
            'true_negatives': class_metrics['true_negatives'],
            'false_negatives': class_metrics['false_negatives'],
            'threshold': threshold,
            'test_samples': len(test_series),
            'predictions_made': n_samples
        }

        logger.info(f"Classification eval ({n_samples} samples): "
                   f"{optimize_metric}={metrics.get(optimize_metric, 0):.4f}, "
                   f"F1={metrics['f1_score']:.4f}, acc={metrics['accuracy']:.4f}")

        return metrics

    def _evaluate_regression(
        self,
        model: Any,
        test_series: Any,
        covariates: Any = None,
        optimize_metric: str = 'mape'
    ) -> Dict[str, float]:
        """Evaluate regression model using simple prediction."""
        n_predict = min(model.output_chunk_length, len(test_series))
        if n_predict <= 0:
            return {'error': 'Test series too short'}

        predictions = model.predict(n=n_predict)
        actuals = test_series[:n_predict]

        if len(predictions) == 0 or len(actuals) == 0:
            return {'error': 'No valid predictions could be made'}

        # Inverse-transform to original scale
        if self.scaler is not None:
            predictions_orig = self.scaler.inverse_transform(predictions)
            actuals_orig = self.scaler.inverse_transform(actuals)
        else:
            predictions_orig = predictions
            actuals_orig = actuals

        actual_values = actuals_orig.values().flatten()

        metrics = {
            'mae': float(mae(actuals_orig, predictions_orig)),
            'rmse': float(rmse(actuals_orig, predictions_orig)),
            'test_samples': len(test_series),
            'predictions_made': len(predictions)
        }

        # MAPE requires strictly positive values
        if actual_values.min() > 0:
            metrics['mape'] = float(mape(actuals_orig, predictions_orig))
        else:
            metrics['mape'] = None
            logger.debug("MAPE skipped - data contains non-positive values")

        logger.info(f"Regression eval: MAE={metrics['mae']:.4f}, RMSE={metrics['rmse']:.4f}")

        return metrics

    def save_model(
        self,
        model: Any,
        model_name: str,
        metadata: Dict = None
    ) -> str:
        """
        Save trained model to disk.

        Args:
            model: Trained Darts model
            model_name: Name for the model file
            metadata: Optional metadata to save alongside

        Returns:
            Path to saved model
        """
        model_path = self.models_dir / f"{model_name}.pt"

        # Note: Callbacks are now serializable (EpochProgressCallback implements
        # __getstate__/__setstate__), so no need to clear them before saving.

        # Save model
        model.save(str(model_path))
        logger.info(f"Model saved to {model_path}")

        # Save metadata
        if metadata:
            meta_path = self.models_dir / f"{model_name}_meta.json"
            with open(meta_path, 'w') as f:
                json.dump(metadata, f, indent=2, default=str)
            logger.info(f"Metadata saved to {meta_path}")

        # Save scaler if available
        if self.scaler:
            scaler_path = self.models_dir / f"{model_name}_scaler.pt"
            if TORCH_AVAILABLE:
                torch.save(self.scaler, str(scaler_path))

        return str(model_path)

    def load_model(self, model_path: str) -> Any:
        """
        Load a trained model from disk.

        Args:
            model_path: Path to model file

        Returns:
            Loaded model
        """
        if not DARTS_AVAILABLE:
            raise RuntimeError("Darts library not available")

        # Determine model type from metadata if available
        meta_path = Path(model_path).with_suffix('').with_name(
            Path(model_path).stem + '_meta.json'
        )

        if meta_path.exists():
            with open(meta_path) as f:
                meta = json.load(f)
                model_type = meta.get('model_type', 'lstm')
        else:
            model_type = 'lstm'

        # Load model based on type
        if model_type == 'nbeats':
            model = NBEATSModel.load(model_path)
        else:
            model = RNNModel.load(model_path)

        logger.info(f"Loaded model from {model_path}")
        return model

    def get_normalization_params(self) -> Optional[Dict[str, Any]]:
        """
        Get the current normalization/scaler parameters.

        For Darts, this returns basic scaler info. The scaler is also saved
        as a separate .pt file alongside the model for torch-based loading.

        Returns:
            Dictionary with scaler params or None if not fitted
        """
        if self.scaler is None:
            return None

        # Darts Scaler uses sklearn internally
        scaler_info = {
            'version': '1.0',
            'type': 'darts_scaler',
            'created_at': datetime.now().isoformat(),
            'description': 'Darts MinMax scaler - load with torch.load(scaler_path)'
        }

        # Try to extract sklearn scaler params
        try:
            if hasattr(self.scaler, 'transformer') and hasattr(self.scaler.transformer, 'data_min_'):
                scaler_info['data_min'] = self.scaler.transformer.data_min_.tolist()
                scaler_info['data_max'] = self.scaler.transformer.data_max_.tolist()
                scaler_info['data_range'] = self.scaler.transformer.data_range_.tolist()
        except Exception:
            pass

        return scaler_info

    def predict(self, model: Any, data: Any, n: int = None, **kwargs) -> Any:
        """
        Generate predictions from a trained Darts model.

        Args:
            model: Trained Darts model
            data: Input series (TimeSeries or can be used as series context)
            n: Number of steps to predict (defaults to model's output_chunk_length)
            **kwargs: Additional prediction options

        Returns:
            Predictions as TimeSeries
        """
        if n is None:
            n = getattr(model, 'output_chunk_length', 1)
        return model.predict(n=n, series=data, **kwargs)


class ModelEvaluator:
    """
    Utility class for model evaluation and metrics.

    For imbalanced datasets (which is common with prediction targets),
    use the ClassificationMetrics from app.services.metrics instead.
    """

    @staticmethod
    def calculate_classification_metrics(
        y_true: np.ndarray,
        y_pred: np.ndarray,
        threshold: float = 0.5
    ) -> Dict[str, float]:
        """
        Calculate classification metrics for binary predictions.

        For comprehensive metrics including AUC-ROC, use:
        from app.services.metrics import ClassificationMetrics
        ClassificationMetrics.calculate_all(y_true, y_pred_proba, threshold)

        Args:
            y_true: True labels
            y_pred: Predicted probabilities
            threshold: Classification threshold

        Returns:
            Dictionary of metrics
        """
        # Import and delegate to new comprehensive metrics module
        try:
            from app.services.metrics import ClassificationMetrics
            return ClassificationMetrics.calculate_all(y_true, y_pred, threshold)
        except ImportError:
            # Fallback to basic implementation
            pass

        y_pred_binary = (y_pred >= threshold).astype(int)

        # True/False Positives/Negatives
        tp = np.sum((y_true == 1) & (y_pred_binary == 1))
        tn = np.sum((y_true == 0) & (y_pred_binary == 0))
        fp = np.sum((y_true == 0) & (y_pred_binary == 1))
        fn = np.sum((y_true == 1) & (y_pred_binary == 0))

        # Calculate metrics
        accuracy = (tp + tn) / (tp + tn + fp + fn) if (tp + tn + fp + fn) > 0 else 0
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        specificity = tn / (tn + fp) if (tn + fp) > 0 else 0
        balanced_accuracy = (recall + specificity) / 2

        # Confusion matrix: [[TN, FP], [FN, TP]]
        confusion_matrix = [
            [int(tn), int(fp)],
            [int(fn), int(tp)]
        ]

        return {
            'accuracy': accuracy,
            'precision': precision,
            'recall': recall,
            'f1_score': f1,
            'balanced_accuracy': balanced_accuracy,
            'true_positives': int(tp),
            'true_negatives': int(tn),
            'false_positives': int(fp),
            'false_negatives': int(fn),
            'confusion_matrix': confusion_matrix
        }

    @staticmethod
    def get_fitness_score(
        y_true: np.ndarray,
        y_pred: np.ndarray,
        metric: str = 'f1_score',
        threshold: float = 0.5
    ) -> float:
        """
        Get a single fitness score for optimization.

        Args:
            y_true: True labels
            y_pred: Predicted probabilities
            metric: One of 'accuracy', 'f1_score', 'precision', 'recall',
                   'balanced_accuracy', 'auc_roc'
            threshold: Classification threshold

        Returns:
            Fitness score (higher is better)
        """
        try:
            from app.services.metrics import ClassificationMetrics
            return ClassificationMetrics.get_fitness_score(y_true, y_pred, metric, threshold)
        except ImportError:
            metrics = ModelEvaluator.calculate_classification_metrics(y_true, y_pred, threshold)
            return metrics.get(metric, 0.0)
