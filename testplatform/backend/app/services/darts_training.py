"""
Darts Training Service

Provides model training, evaluation, and saving functionality.
Integrates with Darts library for timeseries model training.
This service is designed for REGRESSION tasks (time series forecasting).
"""

import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, List, Any, Optional, Sequence, Tuple
import logging
import os
from pathlib import Path
import json

from app.services.data_preparation import bounds_at_dates, common_session_bounds, purged_train_row_count, three_way_bounds
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


from app.services.masked_fill import (  # noqa: E402  (re-exported: the daily mask fill lives in masked_fill)
    all_masked_columns, build_masked_fill, fill_masked_covariates, has_daily_flags, has_masked_cells, masked_groups,
    ticker_key)


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
        self.cov_scaler = None
        # One target scaler per series after a multi-series prepare (index-aligned with the series
        # lists); evaluation inverse-transforms each series with its own scaler.
        self.scalers: List[Any] = []
        self.cov_scalers: List[Any] = []
        # The mask-fill medians of the last three-way prepare (masked_fill.build_masked_fill), saved with the scalers
        self.masked_fill: Optional[Dict[str, Any]] = None
        self._last_fill: Dict[str, float] = {}

    def prepare_data(
        self,
        df: pd.DataFrame,
        target_column: str = 'Close',
        feature_columns: List[str] = None,
        timeframe: str = 'daily',
        scaler_fit_ratio: Optional[float] = None,
        scaler_fit_until: Optional[pd.Timestamp] = None,
        drop_columns: Sequence[str] = (),
        probe_dropped: bool = False
    ) -> Tuple[Any, Any]:
        """
        Prepare data for Darts model training.

        Args:
            df: DataFrame with Date and target columns
            target_column: Column to predict
            feature_columns: Optional covariate columns
            timeframe: Dataset timeframe for frequency inference
            scaler_fit_ratio: Fit the target and covariate scalers on only the
                leading ``ratio`` fraction of the built series, then transform the
                whole series with them. Expressed as a fraction rather than a row
                count so it lands on exactly the same index the caller will split at
                (prepare_data drops NaN-target rows and may fill missing dates, so a
                row count taken from the input frame would not line up). ``None``
                fits on everything, which is only correct when the caller has already
                restricted ``df`` to training rows -- fitting on a frame that still
                contains the held-out tail puts the future's min/max into the
                training transform.
            scaler_fit_until: Same, but the train block is every point dated up to
                and including this timestamp (the common-date split of the
                cross-sectional targets, where the boundary is a date and not a
                fraction of this frame's rows). Wins over ``scaler_fit_ratio``.
            drop_columns: Covariate columns to leave out (the columns dropped for ANOTHER ticker of the job, so every ticker
                has the same covariate width).
            probe_dropped: Only work out which covariate columns the daily mask fill drops for this frame and return that
                list (nothing is scaled or saved).

        ONE window decides the train block of everything fitted here: the first ``fit`` points of the target series (the
        ratio, or the points dated up to ``scaler_fit_until``). The target scaler, the covariate scaler and the median of
        the cells the daily coverage mask blanked (``masked_fill.fill_masked_covariates``) are all fitted on it.

        Returns:
            Tuple of (target_series, covariates_series)
        """
        if not DARTS_AVAILABLE:
            raise RuntimeError("Darts library not available")

        self._last_fill = {}
        df_sorted = df.sort_values('Date').copy()
        df_sorted['Date'] = pd.to_datetime(df_sorted['Date'])
        df_sorted = df_sorted.set_index('Date')

        # Infer frequency based on timeframe
        freq = self._infer_frequency(timeframe, df_sorted)

        # For intraday stock data (1h, 4h, etc.), don't use fill_missing_dates
        # because market data has irregular timestamps (market hours, weekends, holidays)
        # that cannot be filled with a regular frequency grid
        is_intraday = timeframe.lower() in ['1m', '5m', '15m', '30m', '1h', '4h']

        def fit_count(index: pd.Index) -> Optional[int]:
            """Points of the built series that are the train block: the one window the scalers AND the masked-cell medians
            are fitted on; None = all of them."""
            if scaler_fit_until is not None:
                return max(1, int((index <= scaler_fit_until).sum()))
            if scaler_fit_ratio is not None:
                return max(1, int(len(index) * scaler_fit_ratio))
            return None

        # Drop rows with NaN in target column
        if target_column in df_sorted.columns:
            df_sorted = df_sorted.dropna(subset=[target_column])

        # Covariates are point-in-time features: gaps are forward-filled from the PAST only. They
        # used to be ffill().bfill() on the whole frame (+ fillna(0)), which wrote a later value --
        # the first available one, from the future -- into every leading row. Leading rows without a
        # complete covariate vector are dropped (from the target series too, so they stay aligned);
        # a column with no value at all is an error, not a column of zeros.
        # Cells blanked by the daily source's coverage mask are not gaps: they are filled with a train-block median below
        # (after the series exists, so the median's window is the scalers' window), and count as present here.
        cov_raw = None
        available_cols: List[str] = []
        if feature_columns:
            available_cols = [c for c in feature_columns if c in df_sorted.columns and c not in set(drop_columns)]
            if available_cols:
                cov_raw = df_sorted[available_cols]
                probe = cov_raw
                for _, group, flagged in masked_groups(cov_raw):
                    probe = probe.copy()
                    probe.loc[flagged, group] = probe.loc[flagged, group].fillna(0.0)
                complete = probe.ffill().notna().all(axis=1).to_numpy()
                first_complete = int(np.argmax(complete))
                if first_complete > 0:
                    logger.info(f"Dropped {first_complete} leading rows without a complete covariate vector")
                    df_sorted = df_sorted.iloc[first_complete:]
                    cov_raw = cov_raw.iloc[first_complete:]

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
        n_fit = fit_count(target_series.time_index)

        cov_df = None
        if cov_raw is not None:
            # The train block of the medians = the rows of the first n_fit points of the series (by date for a dated series)
            if n_fit is None:
                fill_rows = len(cov_raw)
            elif is_intraday:
                fill_rows = n_fit
            else:
                fill_rows = int((cov_raw.index <= target_series.time_index[n_fit - 1]).sum())
            cov_df, dropped = fill_masked_covariates(cov_raw, fill_rows, self._last_fill)
            available_cols = [c for c in available_cols if c not in dropped]
            if probe_dropped:
                return list(dropped)
            cov_df = cov_df.ffill()
            empty_cols = [c for c in available_cols if cov_df[c].isna().all()]
            if empty_cols:
                raise ValueError(f"Covariate columns without any value: {empty_cols[:5]}")
        elif probe_dropped:
            return []

        # Scale the data. When a train window is given, the scaler sees ONLY the
        # leading (training) slice; the rest of the series is merely transformed by it.
        self.scaler = Scaler()
        if n_fit is None:
            self.scaler.fit(target_series)
        else:
            self.scaler.fit(target_series[:n_fit])
        target_series = self.scaler.transform(target_series)

        # Always convert to float32 for compatibility with loss functions
        # (FocalLoss and other classification losses require float targets)
        target_series = target_series.astype('float32')

        # Create covariates if specified
        covariates = None
        if cov_df is not None:
            # cov_df already covers exactly the rows of the target series (see above)
            if len(cov_df) > 0 and len(cov_df) == len(df_sorted):
                if is_intraday:
                    cov_values = cov_df[available_cols].values
                    covariates = TimeSeries.from_values(cov_values)
                else:
                    covariates = TimeSeries.from_dataframe(
                        cov_df[available_cols],
                        value_cols=available_cols,
                        fill_missing_dates=True,
                        freq=freq
                    )
                # Scale covariates (same train-only window as the target)
                self.cov_scaler = Scaler()
                if n_fit is None:
                    self.cov_scaler.fit(covariates)
                else:
                    self.cov_scaler.fit(covariates[:n_fit])
                covariates = self.cov_scaler.transform(covariates)

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
        timeframe: str = 'daily',
        label_horizon: int = 0
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
            label_horizon: Bars of look-ahead already baked into ``target_column``.
                The last ``label_horizon`` training points are dropped because
                their values are decided by test-period bars.

        Returns:
            Tuple of (train_series, test_series, train_covariates, test_covariates)
        """
        # Prepare the full series, but fit the scalers on the training fraction ONLY.
        # Fitting on the whole frame first (the previous behaviour) let the test
        # block's extrema set the training scale, so the held-out score was not
        # strictly out of sample and did not match causal deployment. The same ratio
        # is used below for split_idx, so the fit window is exactly the train block.
        full_series, full_covariates = self.prepare_data(
            df, target_column, feature_columns, timeframe,
            scaler_fit_ratio=train_ratio
        )

        # Calculate split point
        split_idx = int(len(full_series) * train_ratio)

        # Split series using slicing (preserves index continuity), then purge the
        # training points whose outcome window reaches into the test block.
        train_end = purged_train_row_count(split_idx, label_horizon)
        if train_end < split_idx:
            logger.info(
                f"Purged {split_idx - train_end} boundary training points (label horizon {label_horizon})"
            )
        train_series = full_series[:train_end]
        test_series = full_series[split_idx:]

        # Split covariates if present
        train_covariates = None
        test_covariates = None
        if full_covariates is not None:
            train_covariates = full_covariates[:train_end]
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
        timeframe: str = 'daily', label_horizon: int = 0
    ) -> Tuple[List[Any], List[Any], List[Any], List[Any]]:
        """Prepare and split multiple DataFrames into train/test TimeSeries lists.

        Each series is split, scaled and boundary-purged on its own, so one symbol's
        held-out block never sets another symbol's training scale.
        """
        train_series_list = []
        test_series_list = []
        train_cov_list = []
        test_cov_list = []
        for i, df in enumerate(dataframes):
            train_s, test_s, train_c, test_c = self.prepare_data_split(
                df, train_ratio, target_column, feature_columns, timeframe,
                label_horizon=label_horizon
            )
            train_series_list.append(train_s)
            test_series_list.append(test_s)
            train_cov_list.append(train_c)
            test_cov_list.append(test_c)
        return train_series_list, test_series_list, train_cov_list, test_cov_list

    def prepare_split_three_way(
        self, dataframes: List[pd.DataFrame], train_ratio: float, holdout_share_of_test: float,
        target_column: str, feature_columns: List[str], timeframe: str = 'daily',
        label_horizon: int = 0, common_dates: bool = False
    ) -> Dict[str, Any]:
        """Chronological train / validation / holdout TimeSeries, one series per instrument.

        Each frame (one per ticker) is scaled with scalers fitted on its own training fraction
        and cut with ``three_way_bounds`` (both boundaries purged by ``label_horizon``). With a
        single frame the values are TimeSeries; with several they are lists index-aligned with
        ``self.scalers``. Validation is the GA fitness / early-stopping set, the holdout is only
        scored and reported.

        ``common_dates`` cuts every series at the same DATES (``common_session_bounds`` over the
        frames' sessions) and fits each scaler on the points up to the last train date: the
        cross-sectional daily targets compare the tickers of a session, so one ticker's train
        block must not reach into another's test dates.
        """
        keys = ('train', 'val', 'holdout')
        cb = common_session_bounds(dataframes, train_ratio, holdout_share_of_test, label_horizon) if common_dates else None
        series = {k: [] for k in keys}
        covs = {k: [] for k in keys}
        scalers = []
        cov_scalers = []
        # A group masked in every row of ANY ticker is dropped for ALL of them (one covariate width), and the medians of
        # the masked columns are collected per ticker to be saved with the scalers.
        fit_kw = {'scaler_fit_ratio': train_ratio} if cb is None else {'scaler_fit_until': cb['train_last']}
        all_masked = all_masked_columns(dataframes, feature_columns or [])
        feature_columns = [c for c in (feature_columns or []) if c not in all_masked]
        # Columns the mask fill drops for ONE ticker (nothing unmasked in its train block to take a median of) go for all
        unfillable: set = set()
        if has_daily_flags(feature_columns):
            for df in dataframes:
                unfillable |= set(self.prepare_data(df, target_column, feature_columns, timeframe, probe_dropped=True,
                                                    **fit_kw))
        dropped_for_all = list(dict.fromkeys(all_masked + [c for c in feature_columns if c in unfillable]))
        if dropped_for_all:
            logger.warning(f"Columns the coverage mask leaves without data for at least one ticker are dropped for all: "
                           f"{dropped_for_all}")
            feature_columns = [c for c in feature_columns if c not in unfillable]
        fills: Dict[str, Dict[str, float]] = {}
        for df in dataframes:
            self.cov_scaler = None
            full_series, full_cov = self.prepare_data(df, target_column, feature_columns, timeframe, **fit_kw)
            scalers.append(self.scaler)
            cov_scalers.append(self.cov_scaler)
            fills[ticker_key(df)] = dict(self._last_fill)
            if cb is None:
                b = three_way_bounds(len(full_series), train_ratio, holdout_share_of_test, label_horizon)
            else:
                b = bounds_at_dates(full_series.time_index, cb, f"series {len(scalers)}")
            cuts = {
                'train': slice(0, b['train_end']),
                'val': slice(b['val_start'], b['val_end']),
                'holdout': slice(b['holdout_start'], len(full_series)),
            }
            for k, cut in cuts.items():
                series[k].append(full_series[cut])
                covs[k].append(full_cov[cut] if full_cov is not None else None)
        self.scalers = scalers
        self.scaler = scalers[-1]
        self.cov_scalers = cov_scalers
        self.cov_scaler = cov_scalers[-1]
        self.masked_fill = build_masked_fill(fills, dropped_for_all) if has_daily_flags(feature_columns + dropped_for_all) else None
        logger.info(
            "Three-way split: " + ", ".join(
                f"{k}={sum(len(s) for s in series[k])}" for k in keys
            ) + f" points over {len(dataframes)} series"
        )
        unwrap = (lambda lst: lst[0]) if len(dataframes) == 1 else (lambda lst: lst)
        return {
            'train_series': unwrap(series['train']), 'val_series': unwrap(series['val']),
            'holdout_series': unwrap(series['holdout']),
            'train_covariates': unwrap(covs['train']), 'val_covariates': unwrap(covs['val']),
            'holdout_covariates': unwrap(covs['holdout']),
        }

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

    # Losses that train the network to emit LOGITS (sigmoid is applied to read a probability).
    # Any other loss (mse) trains the output towards the 0/1 target directly, so the output
    # already is the score and a sigmoid on top would squash [0, 1] into [0.5, 0.73].
    LOGIT_LOSSES = ('focal_loss', 'weighted_cross_entropy', 'cross_entropy')
    PROBABILITY_LOSSES = ('mse',)

    def evaluate_model(
        self,
        model: Any,
        test_series: Any,
        covariates: Any = None,
        optimize_metric: str = 'mape',
        threshold: float = 0.5,
        loss_function: Optional[str] = None,
        scalers: Optional[List[Any]] = None
    ) -> Dict[str, float]:
        """
        Evaluate a trained model on a held-out series (or one series per instrument).

        Every point of the series is scored with a rolling one-step-ahead forecast
        (``historical_forecasts``, stride 1) so a metric is computed over the whole block and not
        over the first handful of points. There is no fallback: a block too short for the model, or
        a failed forecast, is an error, never a different (and invalid) kind of evaluation.

        Args:
            model: Trained Darts model
            test_series: TimeSeries, or a list of TimeSeries (multi-instrument; the predictions
                of all series are pooled into one metric)
            covariates: Optional covariate TimeSeries (or list, index-aligned with the series)
            optimize_metric: Metric to optimize ('f1_score', 'accuracy', 'mape', etc.)
            threshold: Classification threshold for binary metrics (default 0.5)
            loss_function: The loss the model was trained with. Required for classification
                metrics: it decides whether the raw output is a logit (sigmoid applied) or
                already a probability-like score (mse; no sigmoid).
            scalers: The target scalers of the series being scored (one per series). Defaults
                to the service's own, which is only right when the service has not prepared
                another set of series since.

        Returns:
            Evaluation metrics
        """
        if not DARTS_AVAILABLE:
            raise RuntimeError("Darts library not available")

        try:
            is_classification = optimize_metric in self.CLASSIFICATION_METRICS
            if is_classification:
                output_is_logit = self._output_is_logit(loss_function)

            multi = isinstance(test_series, list)
            series_list = test_series if multi else [test_series]
            cov_list = covariates if isinstance(covariates, list) else [covariates] * len(series_list)
            if len(cov_list) != len(series_list):
                raise ValueError(f"{len(cov_list)} covariate series for {len(series_list)} test series")
            if scalers is None:
                scalers = self.scalers if multi else [self.scaler]
            if len(scalers) != len(series_list):
                raise ValueError(
                    f"{len(series_list)} test series but {len(scalers)} fitted scalers; "
                    f"prepare the series with prepare_split_three_way first"
                )

            preds, acts = [], []
            for series, cov, scaler in zip(series_list, cov_list, scalers):
                predictions, actuals = self._historical_pairs(model, series, cov)
                if scaler is not None:
                    predictions = scaler.inverse_transform(predictions)
                    actuals = scaler.inverse_transform(actuals)
                preds.append(predictions)
                acts.append(actuals)
            n_test_points = sum(len(s) for s in series_list)

            if is_classification:
                return self._classification_metrics(
                    preds, acts, n_test_points, optimize_metric, threshold, output_is_logit
                )
            return self._regression_metrics(preds, acts, n_test_points)

        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            import traceback
            traceback.print_exc()
            return {'error': str(e)}

    def _output_is_logit(self, loss_function: Optional[str]) -> bool:
        if loss_function in self.LOGIT_LOSSES:
            return True
        if loss_function in self.PROBABILITY_LOSSES:
            return False
        raise ValueError(
            f"loss_function must be one of {self.LOGIT_LOSSES + self.PROBABILITY_LOSSES} to score "
            f"a classification metric, got {loss_function!r}"
        )

    def _historical_pairs(self, model: Any, series: Any, covariates: Any = None) -> Tuple[Any, Any]:
        """Rolling one-step-ahead forecasts over a whole block, with the actuals they target.

        Stride 1 evaluates EVERY point, simulating real-time predictions where at each time T
        we predict T+1 using history up to T.
        """
        input_chunk = model.input_chunk_length
        output_chunk = model.output_chunk_length
        # Gap between the end of the input window and the first forecast step: the look-ahead of the
        # label, so the model never reads a label whose outcome is not yet known (see
        # DartsModelService._apply_output_chunk_shift).
        shift = model.output_chunk_shift if hasattr(model, 'output_chunk_shift') else 0
        min_required = input_chunk + shift + output_chunk
        if len(series) < min_required:
            raise ValueError(
                f"Evaluation block too short ({len(series)} < {min_required} = "
                f"input_chunk + output_chunk_shift + output_chunk)"
            )

        hf_kwargs = {
            'series': series,
            'start': input_chunk + shift,  # Start after enough history
            'forecast_horizon': 1,  # Predict 1 step at a time
            'stride': 1,
            'retrain': False,
            'verbose': False,
            'show_warnings': False,
            'last_points_only': True
        }
        # Add covariates if model was trained with them
        model_uses_covariates = (
            hasattr(model, 'past_covariate_series') and model.past_covariate_series is not None
        )
        if model_uses_covariates and covariates is not None:
            hf_kwargs['past_covariates'] = covariates

        forecasts = model.historical_forecasts(**hf_kwargs)
        if isinstance(forecasts, list):
            if len(forecasts) == 0:
                raise ValueError('No forecasts generated')
            predictions = concatenate(forecasts)
        else:
            predictions = forecasts
        # Actuals matching the prediction time indices
        actuals = series.slice_intersect(predictions)
        if len(predictions) == 0 or len(actuals) == 0:
            raise ValueError('No valid predictions could be made')
        logger.debug(f"Historical forecasts: {len(predictions)} predictions, {len(actuals)} actuals")
        return predictions, actuals

    def _classification_metrics(
        self, preds: List[Any], acts: List[Any], n_test_points: int,
        optimize_metric: str, threshold: float, output_is_logit: bool
    ) -> Dict[str, float]:
        """Classification metrics over the pooled (already inverse-transformed) predictions."""
        from app.services.metrics import ClassificationMetrics

        pred_values = np.concatenate([p.values().flatten() for p in preds])
        actual_values = np.concatenate([a.values().flatten() for a in acts])

        # Logit losses (focal / BCE-with-logits): the network emits logits, so a sigmoid turns
        # them into probabilities. MSE outputs are already ~[0, 1] scores: a sigmoid on those
        # maps everything into [0.5, 0.73] and "predicts positive" for every row at the usual
        # 0.5 threshold, so the metrics describe a constant predictor.
        if output_is_logit:
            pred_proba = 1 / (1 + np.exp(-pred_values))
        else:
            pred_proba = pred_values

        # Ensure predictions are in valid probability range [0, 1]
        pred_proba = np.clip(pred_proba, 0, 1)

        # Actuals should be binary (0 or 1) - round to handle any float noise
        actual_binary = np.round(actual_values).astype(int)

        pred_binary = (pred_proba >= threshold).astype(int)
        n_samples = len(pred_proba)
        logger.debug(f"Classification debug: n_samples={n_samples}, "
                   f"pred_proba range=[{pred_proba.min():.4f}, {pred_proba.max():.4f}], "
                   f"pred_positive={pred_binary.sum()}/{n_samples}, actual_positive={actual_binary.sum()}/{n_samples}")

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
            'test_samples': n_test_points,
            'predictions_made': n_samples
        }

        logger.info(f"Classification eval ({n_samples} samples): "
                   f"{optimize_metric}={metrics.get(optimize_metric, 0):.4f}, "
                   f"F1={metrics['f1_score']:.4f}, acc={metrics['accuracy']:.4f}")
        return metrics

    def _regression_metrics(self, preds: List[Any], acts: List[Any], n_test_points: int) -> Dict[str, float]:
        """MAE / MSE / RMSE / R2 / MAPE over the pooled (already inverse-transformed) predictions.

        Scored on the rolling one-step forecasts of the WHOLE block. This used to forecast
        ``output_chunk_length`` (1-7) points straight after the end of the TRAINING series and
        compare them with the first points of the test block, so the metric came from a handful
        of points and (with any gap between the two) from mismatched timestamps.
        """
        pred_values = np.concatenate([p.values().flatten() for p in preds])
        actual_values = np.concatenate([a.values().flatten() for a in acts])
        err = pred_values - actual_values

        metrics = {
            'mae': float(np.mean(np.abs(err))),
            'mse': float(np.mean(err ** 2)),
            'rmse': float(np.sqrt(np.mean(err ** 2))),
            'test_samples': n_test_points,
            'predictions_made': len(pred_values)
        }
        # R2 = 1 - SS_res / SS_tot is undefined when the actuals are constant
        ss_tot = float(np.sum((actual_values - actual_values.mean()) ** 2))
        if ss_tot > 0:
            metrics['r2'] = float(1.0 - np.sum(err ** 2) / ss_tot)
        else:
            metrics['r2'] = None
            logger.debug("R2 skipped - actual values are constant")
        # MAPE requires strictly positive values
        if actual_values.min() > 0:
            metrics['mape'] = float(np.mean(np.abs(err / actual_values)) * 100)
        else:
            metrics['mape'] = None
            logger.debug("MAPE skipped - data contains non-positive values")

        logger.info(f"Regression eval ({len(pred_values)} points): MAE={metrics['mae']:.4f}, RMSE={metrics['rmse']:.4f}")
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
        if self.cov_scaler:
            cov_scaler_path = self.models_dir / f"{model_name}_cov_scaler.pt"
            if TORCH_AVAILABLE:
                torch.save(self.cov_scaler, str(cov_scaler_path))

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

        # Load model based on type. Every architecture the platform trains has its own class: loading
        # tft/tcn/transformer checkpoints through RNNModel.load fails or mis-restores them.
        from darts.models import TCNModel, TFTModel, TransformerModel
        loaders = {
            'nbeats': NBEATSModel, 'tcn': TCNModel, 'transformer': TransformerModel, 'tft': TFTModel,
            'lstm': RNNModel, 'gru': RNNModel, 'rnn': RNNModel,
        }
        if model_type not in loaders:
            raise ValueError(f"Cannot load model type {model_type!r}; known: {sorted(loaders)}")
        model = loaders[model_type].load(model_path)

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

        return self.describe_scalers(
            self.scalers or [self.scaler], self.cov_scalers or [self.cov_scaler], masked_fill=self.masked_fill
        )

    @staticmethod
    def describe_scalers(scalers: List[Any], cov_scalers: List[Any],
                         masked_fill: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """JSON description of the fitted target AND covariate scalers (one entry per series).

        The covariate scaler used to be dropped entirely, so a reloaded model could not scale its
        features the way it was trained. Top-level data_min/data_max/data_range stay the (last)
        target scaler's for backward compatibility. ``masked_fill`` (``masked_fill.build_masked_fill``) is the
        per-ticker median of every masked column and the dropped all-masked columns: prediction fills from it
        (``masked_fill.apply_saved_fill``) and never refits on the frame it predicts.
        """
        def params(scaler):
            if scaler is None:
                return None
            # The fitted sklearn MinMaxScaler lives in darts' _fitted_params (Scaler.transformer is
            # the unfitted class, which is why the old export silently produced no min/max at all).
            fitted = scaler._fitted_params[0]
            return {
                'data_min': fitted.data_min_.tolist(),
                'data_max': fitted.data_max_.tolist(),
                'data_range': fitted.data_range_.tolist(),
            }

        scaler_info = {
            'version': '1.1',
            'type': 'darts_scaler',
            'created_at': datetime.now().isoformat(),
            'description': 'Darts MinMax scalers (target and covariates, one per series)',
            'target_scalers': [params(s) for s in scalers],
            'covariate_scalers': [params(s) for s in cov_scalers],
        }
        if scalers and scalers[-1] is not None:
            scaler_info.update(params(scalers[-1]))
        if masked_fill is not None:
            scaler_info['masked_fill'] = masked_fill
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
