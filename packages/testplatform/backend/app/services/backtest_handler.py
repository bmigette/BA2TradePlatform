"""
Backtest Handler Service

Executes backtests using backtesting.py library with strategy condition evaluation.

Supports dual-timeframe backtesting:
- Execution data: Higher frequency (e.g., 1-minute) for precise TP/SL
- Prediction data: Lower frequency (e.g., 1-hour) for ML signals

Entry signals are only evaluated when a new prediction bar starts,
while TP/SL and exit conditions are checked on every execution bar.
"""

import bisect
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

import numpy as np
import pandas as pd
from backtesting import Backtest, Strategy

from app.models.database import SessionLocal
from app.models import Dataset, TrainedModel, Strategy as StrategyModel, Backtest as BacktestModel
from app.services.strategy_executor import evaluate_condition_tree, ConfirmationTracker, reset_evaluation_stats, get_evaluation_stats, next_evaluation_bar
from app.services.data_preparation import DataPreparationService
from app.services.tsai_training import TSAITrainingService
from app.services.job_handler import ffill_sparse_indicators
from app.services.perf import perf_timer
# Metric-coercion helpers now live in a lightweight module so the expert backtest path can
# use them without importing THIS module (and its ML training stack). Re-imported here so the
# legacy ML conversion code below keeps using the same single definition.
from app.services.backtest.metrics_utils import _safe_float, _safe_duration_days

logger = logging.getLogger(__name__)


class MLStrategy(Strategy):
    """
    Strategy that uses ML predictions and condition trees for entry/exit decisions.

    Supports dual-timeframe backtesting:
    - Execution data runs at higher frequency (e.g., 1-minute bars)
    - Predictions are from lower frequency (e.g., 1-hour bars)

    Entry signals are only evaluated when a new prediction bar starts,
    while TP/SL is checked on every execution bar for precision.
    """

    # Class-level parameters set before running backtest
    predictions = None  # Dict[timestamp, probability_array]
    prediction_timestamps = None  # Sorted list of prediction timestamps for lookup
    buy_entry_conditions = None
    sell_entry_conditions = None
    exit_conditions = None
    tp_percent = 0.0
    sl_percent = 0.0
    n_classes = 2
    confirmation_tracker = None
    position_sizing_pct = 10.0  # Percent of equity per position

    # Track last trade for "no trade in past X bars/days" conditions
    last_buy_bar_idx = None
    last_buy_date = None
    last_sell_bar_idx = None
    last_sell_date = None
    bar_idx = 0

    # Stats tracking
    buy_trades_opened = 0
    sell_trades_opened = 0

    # Exit reason tracking (class-level for retrieval after bt.run())
    _exit_reasons_result = None  # Dict mapping entry_time -> exit_reason
    _pending_trades_result = None  # Dict mapping entry_time -> {tp_price, sl_price, direction}

    def init(self):
        """Initialize strategy - called once before backtesting starts."""
        self.confirmation_tracker = ConfirmationTracker()
        self.bar_idx = 0
        self.last_buy_bar_idx = None
        self.last_buy_date = None
        self.last_sell_bar_idx = None
        self.last_sell_date = None
        self.buy_trades_opened = 0
        self.sell_trades_opened = 0
        self._logged_context = False
        self._last_pred_timestamp = None  # Track current prediction bar
        self._current_probs = None  # Cache current prediction
        # Exit reason tracking - use class-level dicts so they persist after bt.run()
        MLStrategy._exit_reasons_result = {}
        MLStrategy._pending_trades_result = {}
        self._current_entry_time = None  # Track entry time of current open position

    def _get_prediction_for_time(self, current_time):
        """
        Get the prediction for the current execution bar.

        Uses binary search to find the most recent prediction timestamp
        that is <= current execution time. This allows higher-frequency
        execution data to use lower-frequency predictions.

        Returns:
            tuple: (probs, is_new_bar) where is_new_bar indicates if this is
                   the first execution bar of a new prediction period
        """
        if not self.prediction_timestamps or not self.predictions:
            return None, False

        # Convert to comparable timestamp
        current_ts = pd.Timestamp(current_time)

        # Binary search to find the rightmost prediction timestamp <= current_time
        idx = bisect.bisect_right(self.prediction_timestamps, current_ts) - 1

        if idx < 0:
            # Current time is before all predictions
            return None, False

        pred_timestamp = self.prediction_timestamps[idx]
        probs = self.predictions.get(pred_timestamp)

        # Check if this is a new prediction bar
        is_new_bar = (pred_timestamp != self._last_pred_timestamp)
        self._last_pred_timestamp = pred_timestamp

        return probs, is_new_bar

    def next(self):
        """Called for each bar - evaluate conditions and place orders.

        Optimized for dual-timeframe: only builds full context and evaluates
        entry/exit conditions when there's work to do. On bars without a new
        prediction and no open position, this is essentially a no-op (TP/SL
        are handled by backtesting.py internally).
        """
        current_date = self.data.index[-1]

        # Get prediction for this bar (supports dual-timeframe)
        probs, is_new_prediction_bar = self._get_prediction_for_time(current_date)

        if probs is None:
            self.bar_idx += 1
            return

        # Fast path: no position and not a new prediction bar → nothing to do
        # (TP/SL for existing orders are checked by backtesting.py internally)
        has_position = bool(self.position)
        has_exit_conditions = bool(self.exit_conditions)

        if not has_position and not is_new_prediction_bar:
            self.bar_idx += 1
            return

        # Only check exit conditions when in position and on prediction bars
        # (TP/SL are handled by backtesting.py on every bar automatically)
        if has_position and has_exit_conditions and not is_new_prediction_bar:
            self.bar_idx += 1
            return

        current_price = self.data.Close[-1]

        # Build context (only when needed)
        predicted_class = int(np.argmax(probs))
        max_prob = float(np.max(probs))

        # Calculate bars/days since last trade
        bars_since_last_buy = (self.bar_idx - self.last_buy_bar_idx) if self.last_buy_bar_idx is not None else 999999
        bars_since_last_sell = (self.bar_idx - self.last_sell_bar_idx) if self.last_sell_bar_idx is not None else 999999

        # Days since last trade
        if self.last_buy_date is not None:
            try:
                days_since_last_buy = (current_date - self.last_buy_date).days
            except (TypeError, AttributeError):
                days_since_last_buy = bars_since_last_buy
        else:
            days_since_last_buy = 999999

        if self.last_sell_date is not None:
            try:
                days_since_last_sell = (current_date - self.last_sell_date).days
            except (TypeError, AttributeError):
                days_since_last_sell = bars_since_last_sell
        else:
            days_since_last_sell = 999999

        # Count positions
        buy_count = 1 if self.position.is_long else 0
        sell_count = 1 if self.position.is_short else 0
        total_count = 1 if self.position else 0

        context = {
            'model:prediction': predicted_class,
            'model:predicted_class': predicted_class,
            'model:probability': max_prob,
            'model:max_probability': max_prob,
            'Open': float(self.data.Open[-1]),
            'High': float(self.data.High[-1]),
            'Low': float(self.data.Low[-1]),
            'Close': current_price,
            'Volume': float(self.data.Volume[-1]) if hasattr(self.data, 'Volume') else 0,
            'position:in_position': total_count > 0,
            'position:buy_count': buy_count,
            'position:sell_count': sell_count,
            'position:total_count': total_count,
            'trade:bars_since_last_buy': bars_since_last_buy,
            'trade:bars_since_last_sell': bars_since_last_sell,
            'trade:days_since_last_buy': days_since_last_buy,
            'trade:days_since_last_sell': days_since_last_sell,
        }

        # Add probability and class indicator for each class
        for class_idx in range(len(probs)):
            context[f'model:probability_{class_idx}'] = float(probs[class_idx])
            context[f'model:class_{class_idx}'] = 1 if predicted_class == class_idx else 0

        # Log context fields once
        if not self._logged_context:
            logger.info(f"Available context fields: {sorted(context.keys())}")
            logger.info(f"Buy entry conditions: {self.buy_entry_conditions}")
            logger.info(f"Sell entry conditions: {self.sell_entry_conditions}")
            if self.prediction_timestamps:
                logger.info(f"Dual-timeframe mode: {len(self.prediction_timestamps)} prediction bars")
            self._logged_context = True

        # Check exit conditions for existing position (checked on EVERY execution bar)
        if self.position:
            # Use backtesting.py's built-in P&L percentage
            pnl_pct = self.position.pl_pct * 100  # Convert from decimal to %

            exit_context = context.copy()
            exit_context['position:is_buy'] = self.position.is_long
            exit_context['position:is_sell'] = self.position.is_short
            exit_context['position_pnl_pct'] = pnl_pct

            # Check user-defined exit conditions (TP/SL are handled automatically by backtesting.py)
            for exit_rule in (self.exit_conditions or []):
                conditions = exit_rule.get('conditions', {})
                if evaluate_condition_tree(conditions, exit_context, self.confirmation_tracker, label="Exit"):
                    # Record exit reason with condition details
                    exit_label = exit_rule.get('label') or exit_rule.get('name') or 'Exit condition'
                    # Use tracked entry time as key
                    if self._current_entry_time:
                        entry_key = str(self._current_entry_time)
                        MLStrategy._exit_reasons_result[entry_key] = exit_label
                    self.position.close()
                    self._current_entry_time = None  # Clear after closing
                    break

        # Check entry conditions ONLY on new prediction bars
        # This ensures we only enter when a new ML signal is generated
        if not self.position and is_new_prediction_bar:
            # Check buy entry
            if self.buy_entry_conditions and evaluate_condition_tree(
                self.buy_entry_conditions, context, self.confirmation_tracker, label="BuyEntry"
            ):
                # Calculate TP/SL prices
                tp_price = current_price * (1 + self.tp_percent / 100) if self.tp_percent > 0 else None
                sl_price = current_price * (1 - self.sl_percent / 100) if self.sl_percent > 0 else None

                # Place buy order with TP/SL
                self.buy(size=self.position_sizing_pct / 100, tp=tp_price, sl=sl_price)

                # Track TP/SL for exit reason detection (use class-level for retrieval after bt.run())
                entry_key = str(current_date)
                MLStrategy._pending_trades_result[entry_key] = {
                    'tp_price': tp_price,
                    'sl_price': sl_price,
                    'direction': 'buy',
                    'entry_price': current_price
                }
                self._current_entry_time = current_date

                self.last_buy_bar_idx = self.bar_idx
                self.last_buy_date = current_date
                self.buy_trades_opened += 1

            # Check sell entry
            elif self.sell_entry_conditions and evaluate_condition_tree(
                self.sell_entry_conditions, context, self.confirmation_tracker, label="SellEntry"
            ):
                # Calculate TP/SL prices (inverted for short)
                tp_price = current_price * (1 - self.tp_percent / 100) if self.tp_percent > 0 else None
                sl_price = current_price * (1 + self.sl_percent / 100) if self.sl_percent > 0 else None

                # Place sell order with TP/SL
                self.sell(size=self.position_sizing_pct / 100, tp=tp_price, sl=sl_price)

                # Track TP/SL for exit reason detection (use class-level for retrieval after bt.run())
                entry_key = str(current_date)
                MLStrategy._pending_trades_result[entry_key] = {
                    'tp_price': tp_price,
                    'sl_price': sl_price,
                    'direction': 'sell',
                    'entry_price': current_price
                }
                self._current_entry_time = current_date

                self.last_sell_bar_idx = self.bar_idx
                self.last_sell_date = current_date
                self.sell_trades_opened += 1

        self.bar_idx += 1
        next_evaluation_bar()


def run_backtest(
    model: TrainedModel,
    pred_df: pd.DataFrame,
    exec_df: pd.DataFrame,
    strategy_params: Dict[str, Any],
    initial_capital: float = 10000.0,
    position_sizing_type: str = "fixed",
    position_sizing_value: float = 1000.0,
    commission: float = 0.0,
    slippage: float = 0.0,
    buy_entry_conditions: Optional[Dict] = None,
    sell_entry_conditions: Optional[Dict] = None,
    exit_conditions: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """
    Run a backtest simulation using backtesting.py.

    Args:
        model: TrainedModel record with model file path and params
        pred_df: DataFrame for model predictions (features)
        exec_df: DataFrame for trade execution (OHLCV)
        strategy_params: Strategy configuration
        initial_capital: Starting capital
        position_sizing_type: "fixed" or "percent"
        position_sizing_value: Position size in $ or %
        commission: Commission per trade (%)
        slippage: Slippage per trade (%)
        buy_entry_conditions: Condition tree for buy entries
        sell_entry_conditions: Condition tree for sell entries
        exit_conditions: List of exit condition rules

    Returns:
        Dict with backtest results and metrics
    """
    import torch
    from pathlib import Path
    from app.services.tsai_models import TSAIModelService

    model_type = model.model_type.lower() if model.model_type else 'lstm'

    # Route to Chronos backtest if model is a foundation model
    if model_type.startswith('chronos:'):
        return _run_chronos_backtest(
            model=model,
            pred_df=pred_df,
            exec_df=exec_df,
            strategy_params=strategy_params,
            initial_capital=initial_capital,
            position_sizing_type=position_sizing_type,
            position_sizing_value=position_sizing_value,
            commission=commission,
            slippage=slippage,
            buy_entry_conditions=buy_entry_conditions,
            sell_entry_conditions=sell_entry_conditions,
            exit_conditions=exit_conditions,
        )

    # Get model parameters
    hyperparameters = model.hyperparameters or {}
    seq_len = hyperparameters.get('seq_len', hyperparameters.get('seqLen', 24))
    prediction_mode = model.prediction_mode or 'shift'
    threshold = model.threshold or 0.5

    # Try to load metadata early for feature_columns
    import json
    stored_feature_columns = hyperparameters.get('featureColumns')
    file_path = model.file_path
    file_path_obj = Path(file_path) if file_path else None

    logger.info(f"Model {model.model_id}: file_path={file_path}, featureColumns in hyperparams={stored_feature_columns is not None}")

    # If no stored feature_columns in hyperparameters, try metadata file
    if not stored_feature_columns and file_path_obj:
        if not file_path_obj.exists():
            logger.warning(f"Model file does not exist: {file_path_obj}")
        else:
            meta_patterns = [
                file_path_obj.with_name(file_path_obj.stem + '_meta.json'),
                file_path_obj.with_suffix('.json'),
            ]
            for meta_path in meta_patterns:
                if meta_path.exists():
                    try:
                        with open(meta_path, 'r') as f:
                            meta = json.load(f)
                        stored_feature_columns = meta.get('feature_columns')
                        if stored_feature_columns:
                            logger.info(f"Loaded feature_columns from {meta_path}: {len(stored_feature_columns)} features")
                            break
                    except Exception as e:
                        logger.warning(f"Failed to load metadata from {meta_path}: {e}")

    # Get feature columns - prefer stored columns from training
    c_in = hyperparameters.get('c_in')

    if stored_feature_columns:
        logger.info(f"Using {len(stored_feature_columns)} stored feature columns from training (c_in={c_in})")
        feature_cols = [col for col in stored_feature_columns if col in pred_df.columns]

        if c_in and len(feature_cols) != c_in:
            logger.warning(f"Feature count mismatch: featureColumns has {len(feature_cols)} but model expects c_in={c_in}")

        if not feature_cols:
            logger.error("None of the training features found in dataset")
            return _empty_results(initial_capital)
    else:
        logger.warning(f"No stored feature_columns found for model {model.model_id}. Falling back to computing features.")
        exclude_cols = {'Date', 'target', 'Open', 'High', 'Low', 'Close', 'Volume'}
        feature_cols = [c for c in pred_df.columns if c not in exclude_cols]

    if not feature_cols:
        logger.error("No feature columns found in prediction dataset")
        return _empty_results(initial_capital)

    # Forward-fill sparse indicators (e.g., zigzag)
    pred_df = ffill_sparse_indicators(pred_df)

    # Apply normalization if available
    used_feature_cols = feature_cols
    if model.normalization_params:
        data_prep = DataPreparationService()
        data_prep.load_params(model.normalization_params)
        df_normalized = data_prep.transform(pred_df[feature_cols])
        valid_cols = data_prep.get_valid_columns()
        if valid_cols:
            valid_cols = [c for c in valid_cols if c in df_normalized.columns]
            features = df_normalized[valid_cols].values
            used_feature_cols = valid_cols
            logger.info(f"Using {len(valid_cols)} valid columns after zero-variance filtering")
        else:
            features = df_normalized[feature_cols].values
    else:
        features = pred_df[feature_cols].values

    # Create sliding windows for prediction (timed)
    n_samples = len(features) - seq_len + 1
    if n_samples <= 0:
        logger.error(f"Not enough data for seq_len={seq_len}")
        return _empty_results(initial_capital)

    X = np.array([features[i:i+seq_len] for i in range(n_samples)])
    X = X.transpose(0, 2, 1)  # (samples, features, seq_len) for tsai
    logger.info(f"Created input tensor X with shape {X.shape}")

    # Check for NaN in input features
    nan_count = np.isnan(X).sum()
    if nan_count > 0:
        nan_per_feature = np.isnan(X).any(axis=(0, 2))
        nan_features = [used_feature_cols[i] for i, has_nan in enumerate(nan_per_feature) if has_nan]
        logger.error(f"Input features contain {nan_count} NaN values. Features with NaN: {nan_features[:10]}")
        result = _empty_results(initial_capital)
        result['error'] = f"Input features contain NaN values in: {nan_features[:5]}"
        result['status'] = 'failed'
        return result

    # Load the trained model (timed)
    training_service = TSAITrainingService()
    file_path = model.file_path
    if not file_path:
        logger.error("Model has no file path")
        return _empty_results(initial_capital)

    file_path_obj = Path(file_path)
    if not file_path_obj.exists():
        logger.error(f"Model file not found: {file_path}")
        return _empty_results(initial_capital)

    try:
        if file_path_obj.suffix == '.pkl':
            from tsai.all import load_learner
            learner = load_learner(file_path)
            model_obj = learner.model
        else:
            c_in = hyperparameters.get('c_in')
            c_out = hyperparameters.get('c_out', 2)
            model_params = hyperparameters.get('modelParams', {})

            if c_in is None:
                meta_patterns = [
                    file_path_obj.with_name(file_path_obj.stem + '_meta.json'),
                    file_path_obj.with_suffix('.json'),
                ]
                for meta_path in meta_patterns:
                    if meta_path.exists():
                        try:
                            with open(meta_path, 'r') as f:
                                meta = json.load(f)
                            c_in = meta.get('c_in')
                            if c_out == 2:
                                c_out = meta.get('c_out', c_out)
                            if not model_params:
                                model_params = meta.get('params', {})
                            break
                        except Exception as e:
                            logger.warning(f"Failed to load metadata from {meta_path}: {e}")

            if c_in is None:
                logger.error(f"Cannot determine c_in for model {file_path}")
                return _empty_results(initial_capital)

            model_service = TSAIModelService()
            model_obj = model_service.create_model(
                model_type=model_type,
                params=model_params,
                c_in=c_in,
                c_out=c_out,
                seq_len=seq_len
            )

            state_dict = torch.load(file_path, map_location='cpu', weights_only=True)
            model_obj.load_state_dict(state_dict)
    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        return _empty_results(initial_capital)

    # Run predictions on CPU to avoid MPS compatibility issues with some architectures
    try:
        with perf_timer("backtest.model_inference"):
            model_obj = model_obj.cpu()
            model_obj.train(False)  # Set to evaluation mode
            X_tensor = torch.tensor(X, dtype=torch.float32)
            with torch.no_grad():
                outputs = model_obj(X_tensor)
                if prediction_mode == 'multistep':
                    predictions = torch.sigmoid(outputs).numpy()
                else:
                    predictions = torch.softmax(outputs, dim=1).numpy()
    except Exception as e:
        logger.error(f"Prediction failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return _empty_results(initial_capital)

    # Check for NaN predictions
    nan_count = np.isnan(predictions).sum()
    if nan_count > 0:
        logger.error(f"Predictions contain {nan_count} NaN values")
        result = _empty_results(initial_capital)
        result['error'] = f"Model produced {nan_count} NaN predictions"
        result['status'] = 'failed'
        return result

    # Create prediction lookup by date (using pd.Timestamp for consistent comparison)
    pred_start_idx = seq_len - 1
    pred_dates = pred_df['Date'].iloc[pred_start_idx:pred_start_idx + len(predictions)].values
    pred_timestamps = sorted([pd.Timestamp(d) for d in pred_dates])
    pred_lookup = {ts: predictions[i] for i, ts in enumerate(pred_timestamps)}
    n_classes = predictions.shape[1] if len(predictions.shape) > 1 else 1
    logger.info(f"Predictions shape: {predictions.shape}, n_classes={n_classes}")
    logger.info(f"Prediction timestamps: {len(pred_timestamps)} bars, "
                f"range {pred_timestamps[0]} to {pred_timestamps[-1]}")

    # Use shared strategy backtest runner
    return _run_strategy_backtest(
        pred_lookup=pred_lookup,
        pred_timestamps=pred_timestamps,
        exec_df=exec_df,
        strategy_params=strategy_params,
        initial_capital=initial_capital,
        position_sizing_type=position_sizing_type,
        position_sizing_value=position_sizing_value,
        commission=commission,
        slippage=slippage,
        buy_entry_conditions=buy_entry_conditions,
        sell_entry_conditions=sell_entry_conditions,
        exit_conditions=exit_conditions,
        n_classes=n_classes,
    )


def _run_strategy_backtest(
    pred_lookup: Dict,
    pred_timestamps: list,
    exec_df: pd.DataFrame,
    strategy_params: Dict[str, Any],
    initial_capital: float,
    position_sizing_type: str,
    position_sizing_value: float,
    commission: float,
    slippage: float,
    buy_entry_conditions: Optional[Dict],
    sell_entry_conditions: Optional[Dict],
    exit_conditions: Optional[List[Dict]],
    n_classes: int = 2,
) -> Dict[str, Any]:
    """Shared backtest execution: sets up MLStrategy and runs backtesting.py.

    This is the common code path used by both tsai and Chronos backtests.
    It takes a prediction lookup (timestamp -> probability array) and runs
    the strategy simulation.

    Args:
        pred_lookup: Dict mapping pd.Timestamp -> np.ndarray of probabilities
        pred_timestamps: Sorted list of prediction timestamps
        exec_df: DataFrame with OHLCV + Date columns for trade execution
        strategy_params: Strategy configuration (TP/SL, etc.)
        initial_capital: Starting capital
        position_sizing_type: "fixed" or "percent"
        position_sizing_value: Position size in $ or %
        commission: Commission per trade (%)
        slippage: Slippage per trade (%)
        buy_entry_conditions: Condition tree for buy entries
        sell_entry_conditions: Condition tree for sell entries
        exit_conditions: List of exit condition rules
        n_classes: Number of output classes

    Returns:
        Dict with backtest results and metrics
    """
    # Reset evaluation stats
    reset_evaluation_stats()

    # Prepare OHLCV data for backtesting.py
    bt_data = exec_df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']].copy()
    bt_data.set_index('Date', inplace=True)

    for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
        bt_data[col] = pd.to_numeric(bt_data[col], errors='coerce')
    bt_data = bt_data.dropna()

    # Extract TP/SL from strategy_params
    tp_percent = strategy_params.get('initial_tp_percent') or strategy_params.get('initialTpPercent') or 0
    sl_percent = strategy_params.get('initial_sl_percent') or strategy_params.get('initialSlPercent') or 0
    if tp_percent or sl_percent:
        logger.info(f"TP/SL: TP={tp_percent}%, SL={sl_percent}%")

    # Calculate position sizing
    if position_sizing_type == 'percent':
        position_sizing_pct = position_sizing_value
    else:
        position_sizing_pct = (position_sizing_value / initial_capital) * 100
        position_sizing_pct = min(position_sizing_pct, 99)

    logger.info(f"Position sizing: {position_sizing_pct:.2f}% of equity "
                f"(type={position_sizing_type}, value={position_sizing_value})")

    # Set strategy parameters
    MLStrategy.predictions = pred_lookup
    MLStrategy.prediction_timestamps = pred_timestamps
    MLStrategy.buy_entry_conditions = buy_entry_conditions
    MLStrategy.sell_entry_conditions = sell_entry_conditions
    MLStrategy.exit_conditions = exit_conditions
    MLStrategy.tp_percent = tp_percent
    MLStrategy.sl_percent = sl_percent
    MLStrategy.n_classes = n_classes
    MLStrategy.position_sizing_pct = position_sizing_pct

    # Log timeframe info
    exec_timestamps = bt_data.index
    if len(exec_timestamps) > 1 and len(pred_timestamps) > 1:
        exec_interval = (exec_timestamps[1] - exec_timestamps[0]).total_seconds()
        pred_interval = (pred_timestamps[1] - pred_timestamps[0]).total_seconds()
        logger.info(f"Execution interval: {exec_interval/60:.0f}min, "
                    f"Prediction interval: {pred_interval/60:.0f}min")

    # Run backtest
    bt = Backtest(
        bt_data,
        MLStrategy,
        cash=initial_capital,
        commission=commission / 100,
        exclusive_orders=True,
        trade_on_close=True,
        hedging=False,
    )

    try:
        with perf_timer(f"backtest.strategy_simulation ({len(bt_data)} bars)"):
            stats = bt.run()
    except Exception as e:
        logger.error(f"Backtest execution failed: {e}")
        return _empty_results(initial_capital)

    # Log summary
    eval_stats = get_evaluation_stats()
    logger.info(f"=== Backtest Summary ===")
    logger.info(f"Execution bars: {len(bt_data)}, Prediction bars: {len(pred_timestamps)}")
    logger.info(f"Condition evaluations: {eval_stats['total_evaluations']}")
    logger.info(f"Trades: {stats['# Trades']}, Win Rate: {stats['Win Rate [%]']:.1f}%")
    logger.info(f"Return: {stats['Return [%]']:.2f}%, Max Drawdown: {stats['Max. Drawdown [%]']:.2f}%")

    tree_results = eval_stats.get('tree_results', {})
    if tree_results:
        hit_summary = []
        for label, counts in tree_results.items():
            total = counts['true'] + counts['false']
            hit_rate = counts['true'] / total * 100 if total > 0 else 0
            hit_summary.append(f"{label}: {counts['true']}/{total} ({hit_rate:.1f}%)")
        logger.info(f"Entry/Exit hit rates: {', '.join(hit_summary)}")

    exit_reasons = MLStrategy._exit_reasons_result or {}
    pending_trades = MLStrategy._pending_trades_result or {}

    return _convert_bt_results(stats, bt_data, initial_capital, exit_reasons, pending_trades)


def _run_chronos_backtest(
    model: TrainedModel,
    pred_df: pd.DataFrame,
    exec_df: pd.DataFrame,
    strategy_params: Dict[str, Any],
    initial_capital: float = 10000.0,
    position_sizing_type: str = "fixed",
    position_sizing_value: float = 1000.0,
    commission: float = 0.0,
    slippage: float = 0.0,
    buy_entry_conditions: Optional[Dict] = None,
    sell_entry_conditions: Optional[Dict] = None,
    exit_conditions: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """Run a backtest using a Chronos foundation model for predictions.

    Instead of loading a trained model file, this uses the Chronos pipeline
    to generate rolling forecasts from the price series, then converts
    those forecasts into probability signals for the strategy.

    Args:
        model: TrainedModel record with model_type starting with 'chronos:'
        pred_df: DataFrame with OHLCV + Date columns
        exec_df: DataFrame for trade execution (OHLCV)
        strategy_params: Strategy configuration
        initial_capital: Starting capital
        position_sizing_type: "fixed" or "percent"
        position_sizing_value: Position size in $ or %
        commission: Commission per trade (%)
        slippage: Slippage per trade (%)
        buy_entry_conditions: Condition tree for buy entries
        sell_entry_conditions: Condition tree for sell entries
        exit_conditions: List of exit condition rules

    Returns:
        Dict with backtest results and metrics
    """
    from app.services.chronos_service import run_chronos_inference, CHRONOS_AVAILABLE

    if not CHRONOS_AVAILABLE:
        logger.error("chronos-forecasting is not installed")
        result = _empty_results(initial_capital)
        result['error'] = 'chronos-forecasting is not installed'
        result['status'] = 'failed'
        return result

    hyperparameters = model.hyperparameters or {}
    chronos_model = hyperparameters.get('chronos_model', 'chronos-2')
    prediction_length = hyperparameters.get('prediction_length', 1)
    target_column = 'Close'

    logger.info(f"=== Chronos Backtest ===")
    logger.info(f"Model: {chronos_model}, prediction_length={prediction_length}")
    logger.info(f"Prediction dataset: {len(pred_df)} rows")
    logger.info(f"Execution dataset: {len(exec_df)} rows")

    # Run Chronos inference on the prediction dataset
    try:
        pred_lookup = run_chronos_inference(
            df=pred_df,
            prediction_length=prediction_length,
            target_column=target_column,
            model_name=chronos_model,
        )
    except Exception as e:
        logger.error(f"Chronos inference failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        result = _empty_results(initial_capital)
        result['error'] = f"Chronos inference failed: {e}"
        result['status'] = 'failed'
        return result

    if not pred_lookup:
        logger.error("Chronos inference produced no predictions")
        return _empty_results(initial_capital)

    pred_timestamps = sorted(pred_lookup.keys())
    n_classes = 2  # Chronos signals are binary: [p_down, p_up]

    logger.info(f"Chronos predictions: {len(pred_timestamps)} bars, "
                f"range {pred_timestamps[0]} to {pred_timestamps[-1]}")

    # Use shared strategy backtest runner
    return _run_strategy_backtest(
        pred_lookup=pred_lookup,
        pred_timestamps=pred_timestamps,
        exec_df=exec_df,
        strategy_params=strategy_params,
        initial_capital=initial_capital,
        position_sizing_type=position_sizing_type,
        position_sizing_value=position_sizing_value,
        commission=commission,
        slippage=slippage,
        buy_entry_conditions=buy_entry_conditions,
        sell_entry_conditions=sell_entry_conditions,
        exit_conditions=exit_conditions,
        n_classes=n_classes,
    )


def _convert_bt_results(
    stats,
    bt_data: pd.DataFrame,
    initial_capital: float,
    exit_reasons: Optional[Dict[str, str]] = None,
    pending_trades: Optional[Dict[str, Dict]] = None
) -> Dict[str, Any]:
    """
    Convert backtesting.py stats to our result format.

    Extracts all available metrics from backtesting.py including:
    - Basic metrics: trades, win rate, return, drawdown
    - Risk metrics: Sharpe, Sortino, Calmar ratios
    - Advanced metrics: SQN, expectancy, exposure time
    - Benchmark comparison: Buy & Hold return, Alpha, Beta
    """
    exit_reasons = exit_reasons or {}
    pending_trades = pending_trades or {}

    # Extract trades from stats
    trades_df = stats._trades if hasattr(stats, '_trades') else pd.DataFrame()
    trades_list = []

    if len(trades_df) > 0:
        # Log available keys for debugging
        if pending_trades:
            logger.debug(f"Pending trade keys: {list(pending_trades.keys())[:5]}")
        if exit_reasons:
            logger.debug(f"Exit reason keys: {list(exit_reasons.keys())[:5]}")

        for _, trade in trades_df.iterrows():
            entry_time = trade['EntryTime']
            exit_price = float(trade['ExitPrice'])
            entry_price = float(trade['EntryPrice'])
            direction = 'buy' if trade['Size'] > 0 else 'sell'

            # Determine exit reason - try multiple key formats for robustness
            entry_key = str(entry_time)
            exit_reason = 'unknown'

            # Also try without timezone info for matching
            entry_key_alt = str(pd.Timestamp(entry_time).tz_localize(None)) if hasattr(entry_time, 'tz') else entry_key

            # First check if we recorded an exit condition
            if entry_key in exit_reasons:
                exit_reason = exit_reasons[entry_key]
            elif entry_key_alt in exit_reasons:
                exit_reason = exit_reasons[entry_key_alt]
            else:
                # Check if TP/SL was hit based on exit price
                trade_info = pending_trades.get(entry_key) or pending_trades.get(entry_key_alt)
                if trade_info:
                    tp_price = trade_info.get('tp_price')
                    sl_price = trade_info.get('sl_price')

                    if direction == 'buy':
                        # For long positions: TP is above entry, SL is below
                        if tp_price and exit_price >= tp_price * 0.999:  # Small tolerance for price matching
                            exit_reason = 'Take Profit'
                        elif sl_price and exit_price <= sl_price * 1.001:
                            exit_reason = 'Stop Loss'
                    else:
                        # For short positions: TP is below entry, SL is above
                        if tp_price and exit_price <= tp_price * 1.001:
                            exit_reason = 'Take Profit'
                        elif sl_price and exit_price >= sl_price * 0.999:
                            exit_reason = 'Stop Loss'

            trades_list.append({
                'entry_time': entry_time.isoformat() if hasattr(entry_time, 'isoformat') else str(entry_time),
                'exit_time': trade['ExitTime'].isoformat() if hasattr(trade['ExitTime'], 'isoformat') else str(trade['ExitTime']),
                'direction': direction,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'size': abs(float(trade['Size'])),
                'pnl': float(trade['PnL']),
                'pnl_pct': float(trade['ReturnPct']) * 100,
                'bars_held': int(trade['Duration'].days) if hasattr(trade['Duration'], 'days') else int(trade['Duration']),
                'exit_reason': exit_reason
            })

    # Build equity curve from stats
    equity_curve = []
    if hasattr(stats, '_equity_curve') and stats._equity_curve is not None:
        eq_df = stats._equity_curve
        for idx, row in eq_df.iterrows():
            equity_curve.append({
                'date': idx.isoformat() if hasattr(idx, 'isoformat') else str(idx),
                'equity': float(row['Equity'])
            })
    else:
        # Fallback: just start and end
        equity_curve = [
            {'date': bt_data.index[0].isoformat(), 'equity': initial_capital},
            {'date': bt_data.index[-1].isoformat(), 'equity': _safe_float(stats.get('Equity Final [$]'), initial_capital)}
        ]

    # Build drawdown curve
    drawdown_curve = []
    if hasattr(stats, '_equity_curve') and stats._equity_curve is not None:
        eq_df = stats._equity_curve
        if 'DrawdownPct' in eq_df.columns:
            for idx, row in eq_df.iterrows():
                drawdown_curve.append({
                    'date': idx.isoformat() if hasattr(idx, 'isoformat') else str(idx),
                    'drawdown': float(row['DrawdownPct']) * 100
                })

    # Basic metrics
    total_trades = int(stats['# Trades'])
    win_rate = _safe_float(stats.get('Win Rate [%]'))
    winning_trades = int(total_trades * win_rate / 100) if total_trades > 0 else 0
    losing_trades = total_trades - winning_trades

    # Risk-adjusted metrics (handle Inf for profit factor)
    sharpe = _safe_float(stats.get('Sharpe Ratio'))
    sortino = _safe_float(stats.get('Sortino Ratio'))
    calmar = _safe_float(stats.get('Calmar Ratio'))
    profit_factor = _safe_float(stats.get('Profit Factor'))
    if profit_factor > 999:
        profit_factor = 999.99

    # Duration metrics
    avg_trade_duration = _safe_duration_days(stats.get('Avg. Trade Duration'))
    max_dd_duration = _safe_duration_days(stats.get('Max. Drawdown Duration'))

    return {
        # Basic trade metrics
        'total_trades': total_trades,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'win_rate': round(win_rate, 2),

        # Return metrics
        'total_return': round(_safe_float(stats.get('Return [%]')), 2),
        'annualized_return': round(_safe_float(stats.get('Return (Ann.) [%]')), 2),
        'buy_hold_return': round(_safe_float(stats.get('Buy & Hold Return [%]')), 2),

        # Risk metrics
        'sharpe_ratio': round(sharpe, 2),
        'sortino_ratio': round(sortino, 2),
        'calmar_ratio': round(calmar, 2),
        'volatility': round(_safe_float(stats.get('Volatility (Ann.) [%]')), 2),

        # Drawdown metrics
        'max_drawdown': round(_safe_float(stats.get('Max. Drawdown [%]')), 2),
        'avg_drawdown': round(_safe_float(stats.get('Avg. Drawdown [%]')), 2),
        'max_drawdown_duration': round(max_dd_duration, 1),

        # Trade quality metrics
        'profit_factor': round(profit_factor, 2),
        'expectancy': round(_safe_float(stats.get('Expectancy [%]')), 2),
        'sqn': round(_safe_float(stats.get('SQN')), 2),
        'avg_trade': round(_safe_float(stats.get('Avg. Trade [%]')), 2),
        'best_trade': round(_safe_float(stats.get('Best Trade [%]')), 2),
        'worst_trade': round(_safe_float(stats.get('Worst Trade [%]')), 2),

        # Duration metrics
        'avg_trade_duration': round(avg_trade_duration, 1),
        'exposure_time': round(_safe_float(stats.get('Exposure Time [%]')), 2),

        # Equity metrics
        'final_equity': round(_safe_float(stats.get('Equity Final [$]'), initial_capital), 2),
        'equity_peak': round(_safe_float(stats.get('Equity Peak [$]'), initial_capital), 2),

        # Curves and trades
        'equity_curve': equity_curve,
        'drawdown_curve': drawdown_curve,
        'trades': trades_list
    }


def _empty_results(initial_capital: float) -> Dict[str, Any]:
    """Return empty results structure with all metrics initialized."""
    return {
        # Basic trade metrics
        'total_trades': 0,
        'winning_trades': 0,
        'losing_trades': 0,
        'win_rate': 0.0,

        # Return metrics
        'total_return': 0.0,
        'annualized_return': 0.0,
        'buy_hold_return': 0.0,

        # Risk metrics
        'sharpe_ratio': 0.0,
        'sortino_ratio': 0.0,
        'calmar_ratio': 0.0,
        'volatility': 0.0,

        # Drawdown metrics
        'max_drawdown': 0.0,
        'avg_drawdown': 0.0,
        'max_drawdown_duration': 0.0,

        # Trade quality metrics
        'profit_factor': 0.0,
        'expectancy': 0.0,
        'sqn': 0.0,
        'avg_trade': 0.0,
        'best_trade': 0.0,
        'worst_trade': 0.0,

        # Duration metrics
        'avg_trade_duration': 0.0,
        'exposure_time': 0.0,

        # Equity metrics
        'final_equity': initial_capital,
        'equity_peak': initial_capital,

        # Curves and trades
        'equity_curve': [],
        'drawdown_curve': [],
        'trades': []
    }


def handle_backtest(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a backtest.

    Args:
        task_id: The background task ID
        payload: Dict containing backtest_id and configuration

    Returns:
        Result dict with status and metrics
    """
    backtest_id = payload.get('backtest_id')
    if not backtest_id:
        return {'status': 'failed', 'error': 'backtest_id is required'}

    logger.info(f"Starting backtest execution for backtest_id={backtest_id}")

    db = SessionLocal()
    try:
        # Get backtest record
        backtest = db.query(BacktestModel).filter(BacktestModel.id == backtest_id).first()
        if not backtest:
            return {'status': 'failed', 'error': f'Backtest {backtest_id} not found'}

        # Update status to running
        backtest.status = 'running'
        db.commit()

        # Get related records
        model = db.query(TrainedModel).filter(TrainedModel.id == backtest.model_id).first()
        pred_dataset = db.query(Dataset).filter(Dataset.id == backtest.prediction_dataset_id).first()
        exec_dataset = db.query(Dataset).filter(Dataset.id == backtest.execution_dataset_id).first()

        if not model:
            backtest.status = 'failed'
            backtest.error_message = f'Model {backtest.model_id} not found'
            db.commit()
            return {'status': 'failed', 'error': backtest.error_message}

        if not pred_dataset or not exec_dataset:
            backtest.status = 'failed'
            backtest.error_message = 'Dataset not found'
            db.commit()
            return {'status': 'failed', 'error': backtest.error_message}

        # Get strategy and conditions
        strategy_params = backtest.strategy_params or {}
        buy_entry_conditions = None
        sell_entry_conditions = None
        exit_conditions = None

        if backtest.strategy_id:
            strategy = db.query(StrategyModel).filter(StrategyModel.id == backtest.strategy_id).first()
            if strategy:
                buy_entry_conditions = strategy.buy_entry_conditions
                sell_entry_conditions = strategy.sell_entry_conditions
                exit_conditions = strategy.exit_conditions
                strategy_base_params = {
                    'initial_tp_percent': strategy.initial_tp_percent or 5.0,
                    'initial_sl_percent': strategy.initial_sl_percent or 2.0,
                }
                strategy_params = {**strategy_base_params, **strategy_params}
        else:
            buy_entry_conditions = strategy_params.get('buyEntryConditions') or strategy_params.get('buy_entry_conditions')
            sell_entry_conditions = strategy_params.get('sellEntryConditions') or strategy_params.get('sell_entry_conditions')
            exit_conditions = strategy_params.get('exitConditions') or strategy_params.get('exit_conditions')

        logger.info(f"Strategy conditions loaded: buy={buy_entry_conditions is not None}, sell={sell_entry_conditions is not None}")

        # Load prediction dataset
        try:
            with perf_timer(f"backtest.load_pred_csv ({pred_dataset.file_path})"):
                pred_df = pd.read_csv(pred_dataset.file_path)
                if 'Date' in pred_df.columns:
                    pred_df['Date'] = pd.to_datetime(pred_df['Date'])
        except Exception as e:
            backtest.status = 'failed'
            backtest.error_message = f'Failed to load prediction dataset: {e}'
            db.commit()
            return {'status': 'failed', 'error': backtest.error_message}

        # Load execution dataset
        try:
            with perf_timer(f"backtest.load_exec_csv ({exec_dataset.file_path})"):
                exec_df = pd.read_csv(exec_dataset.file_path)
                if 'Date' in exec_df.columns:
                    exec_df['Date'] = pd.to_datetime(exec_df['Date'])
        except Exception as e:
            backtest.status = 'failed'
            backtest.error_message = f'Failed to load execution dataset: {e}'
            db.commit()
            return {'status': 'failed', 'error': backtest.error_message}

        # Strip timezone from Date columns to avoid comparison issues with naive datetimes
        if 'Date' in pred_df.columns and pred_df['Date'].dt.tz is not None:
            pred_df['Date'] = pred_df['Date'].dt.tz_localize(None)
        if 'Date' in exec_df.columns and exec_df['Date'].dt.tz is not None:
            exec_df['Date'] = exec_df['Date'].dt.tz_localize(None)

        # Filter by date range
        if backtest.start_date:
            pred_df = pred_df[pred_df['Date'] >= backtest.start_date]
            exec_df = exec_df[exec_df['Date'] >= backtest.start_date]
        if backtest.end_date:
            pred_df = pred_df[pred_df['Date'] <= backtest.end_date]
            exec_df = exec_df[exec_df['Date'] <= backtest.end_date]

        # Run the backtest
        results = run_backtest(
            model=model,
            pred_df=pred_df,
            exec_df=exec_df,
            strategy_params=strategy_params,
            initial_capital=backtest.initial_capital,
            position_sizing_type=backtest.position_sizing_type,
            position_sizing_value=backtest.position_sizing_value,
            commission=backtest.commission or 0.0,
            slippage=backtest.slippage or 0.0,
            buy_entry_conditions=buy_entry_conditions,
            sell_entry_conditions=sell_entry_conditions,
            exit_conditions=exit_conditions,
        )

        # Update backtest with results
        backtest.status = 'completed'
        backtest.completed_at = datetime.now()

        # Basic trade metrics
        backtest.total_trades = results['total_trades']
        backtest.winning_trades = results['winning_trades']
        backtest.losing_trades = results['losing_trades']
        backtest.win_rate = results['win_rate']

        # Return metrics
        backtest.total_return = results['total_return']
        backtest.annualized_return = results.get('annualized_return')
        backtest.buy_hold_return = results.get('buy_hold_return')

        # Risk metrics
        backtest.sharpe_ratio = results['sharpe_ratio']
        backtest.sortino_ratio = results.get('sortino_ratio')
        backtest.calmar_ratio = results.get('calmar_ratio')
        backtest.volatility = results.get('volatility')

        # Drawdown metrics
        backtest.max_drawdown = results['max_drawdown']
        backtest.avg_drawdown = results.get('avg_drawdown')
        backtest.max_drawdown_duration = results.get('max_drawdown_duration')

        # Trade quality metrics
        backtest.profit_factor = results['profit_factor']
        backtest.expectancy = results.get('expectancy')
        backtest.sqn = results.get('sqn')
        backtest.avg_trade = results.get('avg_trade')
        backtest.best_trade = results.get('best_trade')
        backtest.worst_trade = results.get('worst_trade')

        # Duration metrics
        backtest.avg_trade_duration = results['avg_trade_duration']
        backtest.exposure_time = results.get('exposure_time')

        # Equity metrics
        backtest.final_equity = results['final_equity']
        backtest.equity_peak = results.get('equity_peak')

        # Curves and trades
        backtest.equity_curve = results['equity_curve']
        backtest.drawdown_curve = results['drawdown_curve']
        backtest.trades = results['trades']

        with perf_timer("backtest.db_commit"):
            db.commit()

        logger.info(f"Backtest {backtest_id} completed: {results['total_trades']} trades")

        return {
            'status': 'completed',
            'backtest_id': backtest_id,
            'results': results
        }

    except Exception as e:
        logger.error(f"Backtest {backtest_id} failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        try:
            backtest = db.query(BacktestModel).filter(BacktestModel.id == backtest_id).first()
            if backtest:
                backtest.status = 'failed'
                backtest.error_message = str(e)
                db.commit()
        except:
            pass
        return {'status': 'failed', 'error': str(e)}
    finally:
        db.close()
