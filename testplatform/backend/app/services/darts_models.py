"""
Darts Models Service

Provides machine learning model architectures using PyTorch and Darts library.
Supports LSTM, GRU, N-BEATS, TCN, Transformer for timeseries forecasting.
This service is designed for REGRESSION tasks (time series forecasting).
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
import logging
import os

from app.services.model_interface import IModelService

logger = logging.getLogger(__name__)

# Check for PyTorch availability
try:
    import torch
    TORCH_AVAILABLE = True
    CUDA_AVAILABLE = torch.cuda.is_available()
    GPU_NAME = torch.cuda.get_device_name(0) if CUDA_AVAILABLE else None
    logger.info(f"PyTorch available. CUDA: {CUDA_AVAILABLE}, GPU: {GPU_NAME}")
except ImportError:
    TORCH_AVAILABLE = False
    CUDA_AVAILABLE = False
    GPU_NAME = None
    logger.warning("PyTorch not available. Install with: pip install torch")

# Check for Darts availability
try:
    from darts import TimeSeries
    from darts.models import (
        RNNModel,
        NBEATSModel,
        TFTModel,
        TCNModel,
        TransformerModel
    )
    from darts.dataprocessing.transformers import Scaler
    DARTS_AVAILABLE = True
    logger.info("Darts library available")
except ImportError:
    DARTS_AVAILABLE = False
    logger.warning("Darts not available. Install with: pip install darts")

# PyTorch Lightning callback for epoch progress updates
try:
    from pytorch_lightning.callbacks import Callback

    class EpochProgressCallback(Callback):
        """Callback to report epoch progress during training with metrics tracking.

        Captures train_loss and val_loss (if validation series is provided during training).
        Note: Classification metrics (accuracy, F1) are computed during evaluation, not training,
        since Darts models are designed for regression and don't compute these natively.

        This callback is SERIALIZABLE - it implements __getstate__/__setstate__ to allow
        the Darts model to be saved even when callbacks are attached.
        The callable reference is excluded from serialization (Darts uses pickle internally).
        """

        def __init__(self, on_epoch_end: callable = None):
            super().__init__()
            self.on_epoch_end_fn = on_epoch_end
            # Store metrics history for access after training
            self.metrics_history = []

        def __getstate__(self):
            """Return serializable state (exclude the callable)."""
            return {'metrics_history': self.metrics_history}

        def __setstate__(self, state):
            """Restore state (callable will be None after loading)."""
            self.on_epoch_end_fn = None
            self.metrics_history = state.get('metrics_history', [])

        def _tensor_to_float(self, value):
            """Convert tensor or numpy value to Python float."""
            if value is None:
                return None
            if hasattr(value, 'item'):
                return float(value.item())
            return float(value)

        def on_train_epoch_end(self, trainer, pl_module):
            current_epoch = trainer.current_epoch + 1  # 0-indexed to 1-indexed
            max_epochs = trainer.max_epochs

            # Collect all available metrics (train_loss, val_loss, etc.)
            metrics = {}
            if trainer.logged_metrics:
                for key, value in trainer.logged_metrics.items():
                    try:
                        metrics[key] = self._tensor_to_float(value)
                    except (TypeError, ValueError):
                        pass  # Skip non-numeric metrics

            # Also check callback_metrics for validation metrics
            if hasattr(trainer, 'callback_metrics') and trainer.callback_metrics:
                for key, value in trainer.callback_metrics.items():
                    if key not in metrics:
                        try:
                            metrics[key] = self._tensor_to_float(value)
                        except (TypeError, ValueError):
                            pass

            # Store in history
            self.metrics_history.append({
                'epoch': current_epoch,
                'max_epochs': max_epochs,
                **metrics
            })

            # Call the callback function if provided
            if self.on_epoch_end_fn:
                try:
                    self.on_epoch_end_fn(current_epoch, max_epochs, metrics)
                except Exception as e:
                    # Don't let callback errors break training
                    logger.warning(f"Epoch callback error: {e}")

    LIGHTNING_CALLBACK_AVAILABLE = True
except ImportError:
    LIGHTNING_CALLBACK_AVAILABLE = False
    EpochProgressCallback = None


class DartsModelService(IModelService):
    """
    Darts-based model service for time series regression/forecasting.

    Supports:
    - LSTM (Long Short-Term Memory)
    - GRU (Gated Recurrent Unit)
    - N-BEATS (Neural Basis Expansion Analysis)
    - TCN (Temporal Convolutional Network)
    - Transformer (Attention-based model)
    - TFT (Temporal Fusion Transformer)
    """

    # Model architecture configurations
    MODEL_ARCHITECTURES = {
        'lstm': {
            'name': 'LSTM',
            'description': 'Long Short-Term Memory network for sequence modeling',
            'default_params': {
                'input_chunk_length': 30,
                'output_chunk_length': 7,
                'hidden_dim': 64,
                'n_rnn_layers': 2,
                'dropout': 0.1,
                'batch_size': 32,
                'n_epochs': 100
            }
        },
        'nbeats': {
            'name': 'N-BEATS',
            'description': 'Neural Basis Expansion Analysis for Time Series',
            'default_params': {
                'input_chunk_length': 30,
                'output_chunk_length': 7,
                'num_stacks': 30,
                'num_blocks': 1,
                'num_layers': 4,
                'layer_widths': 256,
                'batch_size': 32,
                'n_epochs': 100
            }
        },
        'tft': {
            'name': 'Temporal Fusion Transformer',
            'description': 'Transformer-based model for multi-horizon forecasting',
            'default_params': {
                'input_chunk_length': 30,
                'output_chunk_length': 7,
                'hidden_size': 64,
                'lstm_layers': 1,
                'num_attention_heads': 4,
                'dropout': 0.1,
                'batch_size': 32,
                'n_epochs': 100
            }
        },
        'gru': {
            'name': 'GRU',
            'description': 'Gated Recurrent Unit network for sequence modeling',
            'default_params': {
                'input_chunk_length': 30,
                'output_chunk_length': 7,
                'hidden_dim': 64,
                'n_rnn_layers': 2,
                'dropout': 0.1,
                'batch_size': 32,
                'n_epochs': 100
            }
        },
        'tcn': {
            'name': 'TCN',
            'description': 'Temporal Convolutional Network for sequence modeling',
            'default_params': {
                'input_chunk_length': 30,
                'output_chunk_length': 7,
                'kernel_size': 3,
                'num_filters': 64,
                'dilation_base': 2,
                'dropout': 0.1,
                'batch_size': 32,
                'n_epochs': 100
            }
        },
        'transformer': {
            'name': 'Transformer',
            'description': 'Transformer model for time series forecasting',
            'default_params': {
                'input_chunk_length': 30,
                'output_chunk_length': 7,
                'd_model': 64,
                'nhead': 4,
                'num_encoder_layers': 2,
                'num_decoder_layers': 2,
                'dim_feedforward': 128,
                'dropout': 0.1,
                'batch_size': 32,
                'n_epochs': 100
            }
        }
    }

    def __init__(self, use_gpu: bool = True):
        """
        Initialize DartsModelService.

        Args:
            use_gpu: Whether to use GPU if available
        """
        self.use_gpu = use_gpu and CUDA_AVAILABLE
        self.device = 'cuda' if self.use_gpu else 'cpu'

        if self.use_gpu:
            logger.info(f"Using GPU: {GPU_NAME}")
        else:
            logger.info("Using CPU for model training")

    @staticmethod
    def get_system_info() -> Dict[str, Any]:
        """
        Get system information for ML training.

        Returns:
            Dictionary with system capabilities
        """
        info = {
            'pytorch_available': TORCH_AVAILABLE,
            'darts_available': DARTS_AVAILABLE,
            'cuda_available': CUDA_AVAILABLE,
            'gpu_name': GPU_NAME,
            'gpu_memory_total': None,
            'gpu_memory_free': None
        }

        if TORCH_AVAILABLE and CUDA_AVAILABLE:
            try:
                info['gpu_memory_total'] = torch.cuda.get_device_properties(0).total_memory
                info['gpu_memory_free'] = torch.cuda.memory_reserved(0) - torch.cuda.memory_allocated(0)
            except Exception:
                pass

        return info

    def _build_trainer_kwargs(self, epoch_callback: callable = None) -> Dict:
        """Build PyTorch Lightning trainer kwargs with optional epoch callback."""
        kwargs = {
            'accelerator': 'gpu' if self.use_gpu else 'cpu',
            'devices': 1 if self.use_gpu else 'auto'
        }

        # Add epoch progress callback if provided and available
        if epoch_callback and LIGHTNING_CALLBACK_AVAILABLE and EpochProgressCallback:
            kwargs['callbacks'] = [EpochProgressCallback(on_epoch_end=epoch_callback)]

        return kwargs

    def create_lstm_model(self, params: Dict = None, epoch_callback: callable = None, loss_fn: Any = None) -> Any:
        """
        Create LSTM model architecture using Darts.

        Args:
            params: Model parameters (uses defaults if not provided)
                   - hidden_dim: Must be int (Darts RNNModel uses same size for all layers)
            loss_fn: Optional PyTorch loss function

        Returns:
            Darts RNNModel configured as LSTM
        """
        if not DARTS_AVAILABLE:
            raise RuntimeError("Darts library not available")

        p = {**self.MODEL_ARCHITECTURES['lstm']['default_params'], **(params or {})}

        # Darts RNNModel requires hidden_dim to be a single int (same for all layers)
        hidden_dim = p['hidden_dim']
        n_rnn_layers = p['n_rnn_layers']

        # If hidden_dim is a list/tuple, use the first value (or average)
        if isinstance(hidden_dim, (list, tuple)):
            # Use the first value - all RNN layers will have this size
            hidden_dim = int(hidden_dim[0])
            logger.info(f"RNNModel requires int hidden_dim, using first value: {hidden_dim}")

        # RNNModel requires training_length >= input_chunk_length
        # Default training_length is 24, so we set it dynamically
        input_chunk_length = p['input_chunk_length']
        training_length = max(input_chunk_length + 1, 3 * input_chunk_length)

        # Build model kwargs
        model_kwargs = {
            'model': 'LSTM',
            'input_chunk_length': input_chunk_length,
            'output_chunk_length': p['output_chunk_length'],
            'training_length': training_length,
            'hidden_dim': hidden_dim,
            'n_rnn_layers': n_rnn_layers,
            'dropout': p['dropout'],
            'batch_size': p['batch_size'],
            'n_epochs': p['n_epochs'],
            'optimizer_kwargs': {'lr': p.get('learning_rate', 1e-3)},
            'pl_trainer_kwargs': self._build_trainer_kwargs(epoch_callback)
        }

        # Add loss function if provided
        if loss_fn is not None:
            model_kwargs['loss_fn'] = loss_fn
            logger.info(f"Using custom loss function: {type(loss_fn).__name__}")

        model = RNNModel(**model_kwargs)

        logger.info(f"Created LSTM model with params: {p}")
        return model

    def create_nbeats_model(self, params: Dict = None, epoch_callback: callable = None, loss_fn: Any = None) -> Any:
        """
        Create N-BEATS model architecture using Darts.

        Args:
            params: Model parameters (uses defaults if not provided)
                   - layer_widths: Can be int (same for all layers) or list (per-layer)
            loss_fn: Optional PyTorch loss function

        Returns:
            Darts NBEATSModel
        """
        if not DARTS_AVAILABLE:
            raise RuntimeError("Darts library not available")

        p = {**self.MODEL_ARCHITECTURES['nbeats']['default_params'], **(params or {})}

        # Handle layer widths - must be int OR list with length = num_stacks
        layer_widths = p['layer_widths']
        num_stacks = p['num_stacks']
        num_layers = p['num_layers']

        # NBEATS requires layer_widths to be either:
        # - An integer (same width for all stacks)
        # - A list of integers with length = num_stacks
        if isinstance(layer_widths, (list, tuple)):
            if len(layer_widths) != num_stacks:
                # Truncate or extend to match num_stacks
                if len(layer_widths) > num_stacks:
                    layer_widths = list(layer_widths[:num_stacks])
                else:
                    # Extend with last value
                    layer_widths = list(layer_widths) + [layer_widths[-1]] * (num_stacks - len(layer_widths))
            logger.info(f"Using per-stack widths ({len(layer_widths)} for {num_stacks} stacks): {layer_widths[:5]}...")

        # Build model kwargs
        model_kwargs = {
            'input_chunk_length': p['input_chunk_length'],
            'output_chunk_length': p['output_chunk_length'],
            'num_stacks': p['num_stacks'],
            'num_blocks': p['num_blocks'],
            'num_layers': num_layers,
            'layer_widths': layer_widths,
            'batch_size': p['batch_size'],
            'n_epochs': p['n_epochs'],
            'optimizer_kwargs': {'lr': p.get('learning_rate', 1e-3)},
            'pl_trainer_kwargs': self._build_trainer_kwargs(epoch_callback)
        }

        # Add loss function if provided
        if loss_fn is not None:
            model_kwargs['loss_fn'] = loss_fn
            logger.info(f"Using custom loss function: {type(loss_fn).__name__}")

        model = NBEATSModel(**model_kwargs)

        logger.info(f"Created N-BEATS model with params: {p}")
        return model

    def create_gru_model(self, params: Dict = None, epoch_callback: callable = None, loss_fn: Any = None) -> Any:
        """
        Create GRU model architecture using Darts.

        Args:
            params: Model parameters (uses defaults if not provided)
                   - hidden_dim: Must be int (Darts RNNModel uses same size for all layers)
            loss_fn: Optional PyTorch loss function

        Returns:
            Darts RNNModel configured as GRU
        """
        if not DARTS_AVAILABLE:
            raise RuntimeError("Darts library not available")

        p = {**self.MODEL_ARCHITECTURES['gru']['default_params'], **(params or {})}

        # Darts RNNModel requires hidden_dim to be a single int (same for all layers)
        hidden_dim = p['hidden_dim']
        n_rnn_layers = p['n_rnn_layers']

        # If hidden_dim is a list/tuple, use the first value
        if isinstance(hidden_dim, (list, tuple)):
            hidden_dim = int(hidden_dim[0])
            logger.info(f"RNNModel requires int hidden_dim, using first value: {hidden_dim}")

        # RNNModel requires training_length >= input_chunk_length
        input_chunk_length = p['input_chunk_length']
        training_length = max(input_chunk_length + 1, 3 * input_chunk_length)

        # Build model kwargs
        model_kwargs = {
            'model': 'GRU',
            'input_chunk_length': input_chunk_length,
            'output_chunk_length': p['output_chunk_length'],
            'training_length': training_length,
            'hidden_dim': hidden_dim,
            'n_rnn_layers': n_rnn_layers,
            'dropout': p['dropout'],
            'batch_size': p['batch_size'],
            'n_epochs': p['n_epochs'],
            'optimizer_kwargs': {'lr': p.get('learning_rate', 1e-3)},
            'pl_trainer_kwargs': self._build_trainer_kwargs(epoch_callback)
        }

        # Add loss function if provided
        if loss_fn is not None:
            model_kwargs['loss_fn'] = loss_fn
            logger.info(f"Using custom loss function: {type(loss_fn).__name__}")

        model = RNNModel(**model_kwargs)

        logger.info(f"Created GRU model with params: {p}")
        return model

    def create_tcn_model(self, params: Dict = None, epoch_callback: callable = None, loss_fn: Any = None) -> Any:
        """
        Create TCN (Temporal Convolutional Network) model using Darts.

        Args:
            params: Model parameters (uses defaults if not provided)
            loss_fn: Optional PyTorch loss function

        Returns:
            Darts TCNModel
        """
        if not DARTS_AVAILABLE:
            raise RuntimeError("Darts library not available")

        p = {**self.MODEL_ARCHITECTURES['tcn']['default_params'], **(params or {})}

        # Build model kwargs
        model_kwargs = {
            'input_chunk_length': p['input_chunk_length'],
            'output_chunk_length': p['output_chunk_length'],
            'kernel_size': p.get('kernel_size', 3),
            'num_filters': p.get('num_filters', 64),
            'dilation_base': p.get('dilation_base', 2),
            'dropout': p['dropout'],
            'batch_size': p['batch_size'],
            'n_epochs': p['n_epochs'],
            'optimizer_kwargs': {'lr': p.get('learning_rate', 1e-3)},
            'pl_trainer_kwargs': self._build_trainer_kwargs(epoch_callback)
        }

        # Add loss function if provided
        if loss_fn is not None:
            model_kwargs['loss_fn'] = loss_fn
            logger.info(f"Using custom loss function: {type(loss_fn).__name__}")

        model = TCNModel(**model_kwargs)

        logger.info(f"Created TCN model with params: {p}")
        return model

    def create_transformer_model(self, params: Dict = None, epoch_callback: callable = None, loss_fn: Any = None) -> Any:
        """
        Create Transformer model using Darts.

        Args:
            params: Model parameters (uses defaults if not provided)
            loss_fn: Optional PyTorch loss function

        Returns:
            Darts TransformerModel
        """
        if not DARTS_AVAILABLE:
            raise RuntimeError("Darts library not available")

        p = {**self.MODEL_ARCHITECTURES['transformer']['default_params'], **(params or {})}

        # Ensure d_model is divisible by nhead (required by Transformer)
        d_model = p.get('d_model', 64)
        nhead = p.get('nhead', 4)
        if d_model % nhead != 0:
            # Adjust d_model to be divisible by nhead
            d_model = ((d_model // nhead) + 1) * nhead
            logger.info(f"Adjusted d_model to {d_model} to be divisible by nhead={nhead}")

        # Build model kwargs
        model_kwargs = {
            'input_chunk_length': p['input_chunk_length'],
            'output_chunk_length': p['output_chunk_length'],
            'd_model': d_model,
            'nhead': nhead,
            'num_encoder_layers': p.get('num_encoder_layers', 2),
            'num_decoder_layers': p.get('num_decoder_layers', 2),
            'dim_feedforward': p.get('dim_feedforward', 128),
            'dropout': p['dropout'],
            'batch_size': p['batch_size'],
            'n_epochs': p['n_epochs'],
            'optimizer_kwargs': {'lr': p.get('learning_rate', 1e-3)},
            'pl_trainer_kwargs': self._build_trainer_kwargs(epoch_callback)
        }

        # Add loss function if provided
        if loss_fn is not None:
            model_kwargs['loss_fn'] = loss_fn
            logger.info(f"Using custom loss function: {type(loss_fn).__name__}")

        model = TransformerModel(**model_kwargs)

        logger.info(f"Created Transformer model with params: {p}")
        return model

    def create_tft_model(self, params: Dict = None, epoch_callback: callable = None, loss_fn: Any = None) -> Any:
        """
        Create TFT (Temporal Fusion Transformer) model using Darts.

        TFT combines LSTM with attention for multi-horizon forecasting.
        Developed by Google, provides interpretable outputs.

        Args:
            params: Model parameters (uses defaults if not provided)
            loss_fn: Optional PyTorch loss function

        Returns:
            Darts TFTModel
        """
        if not DARTS_AVAILABLE:
            raise RuntimeError("Darts library not available")

        p = {**self.MODEL_ARCHITECTURES['tft']['default_params'], **(params or {})}

        # Build model kwargs
        model_kwargs = {
            'input_chunk_length': p['input_chunk_length'],
            'output_chunk_length': p['output_chunk_length'],
            'hidden_size': p.get('hidden_size', 64),
            'lstm_layers': p.get('lstm_layers', 1),
            'num_attention_heads': p.get('num_attention_heads', 4),
            'dropout': p['dropout'],
            'batch_size': p['batch_size'],
            'n_epochs': p['n_epochs'],
            'add_relative_index': True,  # Auto-generate future covariates from time index
            'optimizer_kwargs': {'lr': p.get('learning_rate', 1e-3)},
            'pl_trainer_kwargs': self._build_trainer_kwargs(epoch_callback)
        }

        # Add loss function if provided
        if loss_fn is not None:
            model_kwargs['loss_fn'] = loss_fn
            logger.info(f"Using custom loss function: {type(loss_fn).__name__}")

        model = TFTModel(**model_kwargs)

        logger.info(f"Created TFT model with params: {p}")
        return model

    def create_model(self, model_type: str, params: Dict = None, epoch_callback: callable = None, loss_fn: Any = None) -> Any:
        """
        Create a model of the specified type.

        Args:
            model_type: One of 'lstm', 'nbeats', 'gru', 'tcn', 'transformer', 'tft'
            params: Model parameters
            epoch_callback: Optional callback function(current_epoch, total_epochs) called after each epoch
            loss_fn: Optional PyTorch loss function for training (e.g., FocalLoss, WeightedBCELoss)

        Returns:
            Configured Darts model
        """
        model_type = model_type.lower()

        creators = {
            'lstm': self.create_lstm_model,
            'nbeats': self.create_nbeats_model,
            'gru': self.create_gru_model,
            'tcn': self.create_tcn_model,
            'transformer': self.create_transformer_model,
            'tft': self.create_tft_model
        }

        if model_type not in creators:
            raise ValueError(f"Unknown model type: {model_type}. Supported: {list(creators.keys())}")

        return creators[model_type](params, epoch_callback=epoch_callback, loss_fn=loss_fn)

    @staticmethod
    def get_available_models() -> Dict[str, Dict]:
        """
        Get list of available model architectures.

        Returns:
            Dictionary of model configurations
        """
        return DartsModelService.MODEL_ARCHITECTURES.copy()

    def get_parameter_ranges(self, model_type: str) -> Dict[str, List]:
        """
        Get hyperparameter ranges for genetic optimization.

        Args:
            model_type: Model architecture name

        Returns:
            Dictionary mapping param names to valid value ranges
        """
        model_type = model_type.lower()
        if model_type not in self.MODEL_ARCHITECTURES:
            raise ValueError(f"Unknown model type: {model_type}")

        # Return param ranges from the hyperparameter ranges defined in MODEL_ARCHITECTURES
        arch = self.MODEL_ARCHITECTURES[model_type]
        return arch.get('hyperparameter_ranges', {})

    def apply_layer_size_factor(self, params: Dict, factor: float) -> Dict:
        """
        Scale layer size parameters by a factor.

        Args:
            params: Original parameters
            factor: Scaling factor (e.g., 0.5, 1.0, 2.0)

        Returns:
            Scaled parameters
        """
        scaled = params.copy()
        size_params = ['hidden_dim', 'layer_widths', 'd_model', 'dim_feedforward',
                       'hidden_size', 'num_filters']
        for key in size_params:
            if key in scaled:
                if isinstance(scaled[key], (list, tuple)):
                    scaled[key] = [int(v * factor) for v in scaled[key]]
                else:
                    scaled[key] = int(scaled[key] * factor)
        return scaled


# Import BARS_PER_DAY from central location to avoid duplication
from app.services.dataset_handler import BARS_PER_DAY


def days_to_bars(days: int, timeframe: str) -> int:
    """
    Convert days to bars based on timeframe.

    Args:
        days: Number of days
        timeframe: Dataset timeframe (e.g., '1h', '15m', '1d')

    Returns:
        Number of bars equivalent to the specified days
    """
    bars_per_day = BARS_PER_DAY.get(timeframe, 1)
    return max(1, round(days * bars_per_day))


class PredictionTargetService:
    """
    Service for calculating prediction targets for ML training.

    Creates binary classification targets like:
    - price_up_10pct_5dd_7d: Price goes up 10% with max 5% drawdown in 7 days
    - price_down_10pct_5dd_7d: Price goes down 10% with max 5% drawup in 7 days
    """

    def __init__(self):
        pass

    def calculate_prediction_targets(
        self,
        df: pd.DataFrame,
        targets: List[Dict[str, Any]]
    ) -> pd.DataFrame:
        """
        Calculate prediction targets for a dataset.

        Args:
            df: DataFrame with Date, Close columns
            targets: List of target configurations, e.g.:
                [{'profit_pct': 10, 'max_dd': 5, 'days': 7, 'direction': 'up'}]

        Returns:
            DataFrame with target columns added
        """
        result_df = df.copy()
        result_df = result_df.sort_values('Date').reset_index(drop=True)

        for target in targets:
            profit_pct = target.get('profit_pct', 10)
            max_dd = target.get('max_dd', 5)
            days = target.get('days', 7)
            direction = target.get('direction', 'up')

            col_name = f"price_{direction}_{profit_pct}pct_{max_dd}dd_{days}d"

            result_df[col_name] = self._calculate_single_target(
                result_df, profit_pct, max_dd, days, direction
            )

            logger.info(f"Calculated target: {col_name}")

        return result_df

    def _calculate_single_target(
        self,
        df: pd.DataFrame,
        profit_pct: float,
        max_dd: float,
        days: int,
        direction: str
    ) -> pd.Series:
        """
        Calculate a single prediction target using High/Low prices bar-by-bar.

        For each bar, simulates entering at Close price, then checks subsequent
        bars' High/Low to determine if profit target is reached before drawdown
        limit is breached.

        Args:
            df: DataFrame with Close, High, Low columns
            profit_pct: Required profit percentage to hit
            max_dd: Maximum drawdown allowed before profit target
            days: Maximum bars to look ahead
            direction: 'up' (buy order) or 'down' (sell order)

        Returns:
            Series with 1 where target is met, 0 otherwise
        """
        n = len(df)
        targets = np.zeros(n)

        close_prices = df['Close'].values
        high_prices = df['High'].values
        low_prices = df['Low'].values

        # Calculate price thresholds
        for i in range(n - 1):  # Need at least 1 future bar
            entry_price = close_prices[i]

            # Calculate target and stop prices
            if direction == 'up':
                # BUY order: profit from price going up, drawdown from price going down
                profit_target = entry_price * (1 + profit_pct / 100)
                stop_price = entry_price * (1 - max_dd / 100)
            else:
                # SELL order: profit from price going down, drawdown from price going up
                profit_target = entry_price * (1 - profit_pct / 100)
                stop_price = entry_price * (1 + max_dd / 100)

            # Check each subsequent bar up to 'days' bars ahead
            max_bars = min(days, n - i - 1)
            for j in range(1, max_bars + 1):
                bar_idx = i + j
                bar_high = high_prices[bar_idx]
                bar_low = low_prices[bar_idx]

                if direction == 'up':
                    # For BUY: check if stopped out first (low breaches stop)
                    # then check if profit target hit (high reaches target)
                    # Within same bar, assume stop checked before profit (conservative)
                    if bar_low <= stop_price:
                        # Stopped out - drawdown exceeded before profit
                        break
                    if bar_high >= profit_target:
                        # Profit target hit without exceeding drawdown
                        targets[i] = 1
                        break
                else:
                    # For SELL: check if stopped out first (high breaches stop)
                    # then check if profit target hit (low reaches target)
                    if bar_high >= stop_price:
                        # Stopped out - drawup exceeded before profit
                        break
                    if bar_low <= profit_target:
                        # Profit target hit without exceeding drawup
                        targets[i] = 1
                        break

        return pd.Series(targets, index=df.index)

    def create_symmetric_targets(
        self,
        df: pd.DataFrame,
        profit_pct: float = 10,
        max_dd: float = 5,
        days: int = 7
    ) -> pd.DataFrame:
        """
        Create symmetric up/down prediction targets.

        Args:
            df: DataFrame with Date, Close columns
            profit_pct: Profit target percentage
            max_dd: Maximum drawdown percentage
            days: Time horizon in days

        Returns:
            DataFrame with both up and down target columns
        """
        targets = [
            {'profit_pct': profit_pct, 'max_dd': max_dd, 'days': days, 'direction': 'up'},
            {'profit_pct': profit_pct, 'max_dd': max_dd, 'days': days, 'direction': 'down'}
        ]

        return self.calculate_prediction_targets(df, targets)

    def verify_symmetry(self, df: pd.DataFrame, targets: List[Dict]) -> bool:
        """
        Verify that prediction targets maintain symmetry constraint.

        Args:
            df: DataFrame with target columns
            targets: List of target configurations

        Returns:
            True if symmetry is maintained
        """
        up_targets = [t for t in targets if t.get('direction') == 'up']
        down_targets = [t for t in targets if t.get('direction') == 'down']

        if len(up_targets) != len(down_targets):
            return False

        # Check that each up target has a matching down target
        for up in up_targets:
            matching_down = False
            for down in down_targets:
                if (up['profit_pct'] == down['profit_pct'] and
                    up['max_dd'] == down['max_dd'] and
                    up['days'] == down['days']):
                    matching_down = True
                    break
            if not matching_down:
                return False

        return True

    def calculate_directional(
        self,
        df: pd.DataFrame,
        horizon: int,
        direction: str
    ) -> pd.Series:
        """
        Calculate directional movement target.

        Binary classification: Will price be higher or lower in N bars?

        Args:
            df: DataFrame with 'Close' column
            horizon: Number of bars ahead to predict
            direction: 'up' (price higher) or 'down' (price lower)

        Returns:
            Series with 1 if condition met, 0 otherwise
        """
        n = len(df)
        targets = np.zeros(n)
        close = df['Close'].values

        for i in range(n - horizon):
            future_price = close[i + horizon]
            current_price = close[i]

            if direction == 'up':
                targets[i] = 1 if future_price > current_price else 0
            else:
                targets[i] = 1 if future_price < current_price else 0

        # Last 'horizon' bars are undefined
        targets[n - horizon:] = np.nan

        col_name = f"directional_{direction}_{horizon}b"
        return pd.Series(targets, index=df.index, name=col_name)

    def calculate_triple_barrier(
        self,
        df: pd.DataFrame,
        profit_pct: float,
        stop_pct: float,
        max_bars: int
    ) -> pd.Series:
        """
        Calculate Triple-Barrier target (Marcos Lopez de Prado method).

        Multi-class: 0=stop hit, 1=profit hit, 2=timeout

        Args:
            df: DataFrame with 'Close', 'High', 'Low' columns
            profit_pct: Upper barrier profit percentage
            stop_pct: Lower barrier stop-loss percentage
            max_bars: Vertical barrier (max bars before timeout)

        Returns:
            Series with 0 (stop), 1 (profit), or 2 (timeout)
        """
        n = len(df)
        targets = np.full(n, np.nan)

        close = df['Close'].values
        high = df['High'].values
        low = df['Low'].values

        for i in range(n - 1):
            entry_price = close[i]
            profit_target = entry_price * (1 + profit_pct / 100)
            stop_price = entry_price * (1 - stop_pct / 100)

            max_look = min(max_bars, n - i - 1)
            result = 2  # Default: timeout

            for j in range(1, max_look + 1):
                bar_idx = i + j
                bar_high = high[bar_idx]
                bar_low = low[bar_idx]

                # Check stop first (conservative)
                if bar_low <= stop_price:
                    result = 0  # Stop hit
                    break
                if bar_high >= profit_target:
                    result = 1  # Profit hit
                    break

            targets[i] = result

        col_name = f"barrier_{profit_pct}p_{stop_pct}s_{max_bars}b"
        return pd.Series(targets, index=df.index, name=col_name)

    def calculate_trend_reversal(
        self,
        df: pd.DataFrame,
        indicator: str,
        indicator_params: dict,
        threshold: float,
        direction: str
    ) -> pd.Series:
        """
        Calculate trend reversal target based on technical indicators.

        Binary classification: Detects potential trend reversals.

        Args:
            df: DataFrame with OHLC columns
            indicator: 'rsi', 'macd', 'sar', or 'zigzag'
            indicator_params: Indicator-specific parameters
            threshold: Threshold value for signal detection
            direction: 'bullish' (buy signal) or 'bearish' (sell signal)

        Returns:
            Series with 1 where reversal detected, 0 otherwise
        """
        from app.services.indicators import IndicatorService

        n = len(df)
        targets = np.zeros(n)
        indicator_service = IndicatorService()

        if indicator == 'rsi':
            period = indicator_params.get('period')
            if period is None:
                raise ValueError("rsi indicator requires 'period' parameter")
            rsi = indicator_service.calculate_rsi(df, period)

            if direction == 'bullish':
                # Bullish reversal: RSI crosses above oversold threshold
                for i in range(1, n):
                    if not pd.isna(rsi.iloc[i]) and not pd.isna(rsi.iloc[i-1]):
                        if rsi.iloc[i-1] <= threshold and rsi.iloc[i] > threshold:
                            targets[i] = 1
            else:
                # Bearish reversal: RSI crosses below overbought threshold
                overbought = 100 - threshold
                for i in range(1, n):
                    if not pd.isna(rsi.iloc[i]) and not pd.isna(rsi.iloc[i-1]):
                        if rsi.iloc[i-1] >= overbought and rsi.iloc[i] < overbought:
                            targets[i] = 1

        elif indicator == 'macd':
            fast = indicator_params.get('fast')
            slow = indicator_params.get('slow')
            signal_period = indicator_params.get('signal')
            if fast is None or slow is None or signal_period is None:
                raise ValueError("macd indicator requires 'fast', 'slow', and 'signal' parameters")
            macd_data = indicator_service.calculate_macd(df, fast, slow, signal_period)
            macd_line = macd_data['macd']
            signal_line = macd_data['signal']

            if direction == 'bullish':
                # Bullish: MACD crosses above signal line
                for i in range(1, n):
                    if not pd.isna(macd_line.iloc[i]) and not pd.isna(signal_line.iloc[i]):
                        prev_diff = macd_line.iloc[i-1] - signal_line.iloc[i-1]
                        curr_diff = macd_line.iloc[i] - signal_line.iloc[i]
                        if prev_diff <= 0 and curr_diff > 0:
                            targets[i] = 1
            else:
                # Bearish: MACD crosses below signal line
                for i in range(1, n):
                    if not pd.isna(macd_line.iloc[i]) and not pd.isna(signal_line.iloc[i]):
                        prev_diff = macd_line.iloc[i-1] - signal_line.iloc[i-1]
                        curr_diff = macd_line.iloc[i] - signal_line.iloc[i]
                        if prev_diff >= 0 and curr_diff < 0:
                            targets[i] = 1

        elif indicator == 'sar':
            # Handle both snake_case and camelCase parameter names
            af_start = indicator_params.get('af_start') or indicator_params.get('afStart')
            af_max = indicator_params.get('af_max') or indicator_params.get('afMax')
            if af_start is None or af_max is None:
                raise ValueError("sar indicator requires 'af_start'/'afStart' and 'af_max'/'afMax' parameters")
            sar = indicator_service.calculate_sar(df, af_start, af_max)
            close = df['Close'].values

            if direction == 'bullish':
                # Bullish: Price crosses above SAR
                for i in range(1, n):
                    if not pd.isna(sar.iloc[i]):
                        if close[i-1] < sar.iloc[i-1] and close[i] > sar.iloc[i]:
                            targets[i] = 1
            else:
                # Bearish: Price crosses below SAR
                for i in range(1, n):
                    if not pd.isna(sar.iloc[i]):
                        if close[i-1] > sar.iloc[i-1] and close[i] < sar.iloc[i]:
                            targets[i] = 1

        elif indicator == 'zigzag':
            # Handle both snake_case (backend) and camelCase (frontend) parameter names
            deviation_pct = indicator_params.get('deviation_pct') or indicator_params.get('deviationPct')
            if deviation_pct is None:
                raise ValueError("zigzag indicator requires 'deviation_pct' or 'deviationPct' parameter")
            zigzag = indicator_service.calculate_zigzag(df, deviation_pct)

            # The zigzag is interpolated, so we detect direction changes
            # by looking at where the slope changes sign
            # Bullish reversal: zigzag was going DOWN and starts going UP (low pivot)
            # Bearish reversal: zigzag was going UP and starts going DOWN (high pivot)

            for i in range(2, n - 1):
                if pd.isna(zigzag.iloc[i]) or pd.isna(zigzag.iloc[i-1]) or pd.isna(zigzag.iloc[i+1]):
                    continue

                # Calculate slopes before and after current point
                prev_slope = zigzag.iloc[i] - zigzag.iloc[i-1]
                next_slope = zigzag.iloc[i+1] - zigzag.iloc[i]

                if direction == 'bullish':
                    # Bullish: Was going DOWN (prev_slope < 0), now going UP (next_slope > 0)
                    # This is a LOW pivot point
                    if prev_slope < 0 and next_slope > 0:
                        targets[i] = 1
                else:
                    # Bearish: Was going UP (prev_slope > 0), now going DOWN (next_slope < 0)
                    # This is a HIGH pivot point
                    if prev_slope > 0 and next_slope < 0:
                        targets[i] = 1

        col_name = f"reversal_{indicator}_{direction}"
        return pd.Series(targets, index=df.index, name=col_name)

    def calculate_volatility(
        self,
        df: pd.DataFrame,
        horizon: int,
        method: str
    ) -> pd.Series:
        """
        Calculate volatility target (regression).

        Predicts realized volatility for the next N periods.

        Args:
            df: DataFrame with 'Close', 'High', 'Low' columns
            horizon: Number of periods ahead
            method: 'std' (standard deviation), 'range' (high-low range),
                    or 'atr' (average true range)

        Returns:
            Series with volatility values (continuous)
        """
        n = len(df)
        targets = np.full(n, np.nan)
        close = df['Close'].values
        high = df['High'].values
        low = df['Low'].values

        for i in range(n - horizon):
            future_slice = slice(i + 1, i + horizon + 1)

            if method == 'std':
                # Standard deviation of returns
                future_prices = close[i:i + horizon + 1]
                if len(future_prices) > 1:
                    returns = np.diff(future_prices) / future_prices[:-1]
                    targets[i] = np.std(returns) * 100  # As percentage
            elif method == 'range':
                # Average high-low range
                ranges = high[future_slice] - low[future_slice]
                targets[i] = np.mean(ranges / close[i]) * 100  # As percentage of entry price
            elif method == 'atr':
                # Average True Range approximation
                tr_values = []
                for j in range(i + 1, min(i + horizon + 1, n)):
                    tr = max(
                        high[j] - low[j],
                        abs(high[j] - close[j-1]) if j > 0 else high[j] - low[j],
                        abs(low[j] - close[j-1]) if j > 0 else high[j] - low[j]
                    )
                    tr_values.append(tr)
                if tr_values:
                    targets[i] = (np.mean(tr_values) / close[i]) * 100  # As percentage

        col_name = f"volatility_{method}_{horizon}b"
        return pd.Series(targets, index=df.index, name=col_name)

    def calculate_all_targets(
        self,
        df: pd.DataFrame,
        targets_config: List[Dict[str, Any]],
        dataset_timeframe: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Calculate all target types and return with statistics.

        Supports multi-timeframe targets: if a target has a 'timeframe' field
        different from the dataset's base timeframe, the indicator will be
        calculated on the higher timeframe and aligned back to the base.

        Args:
            df: DataFrame with OHLC columns
            targets_config: List of target configurations, each may have:
                - timeframe: Optional[str] - Calculate target on this timeframe
            dataset_timeframe: Base timeframe of the dataset (e.g., '15m', '1h')

        Returns:
            List of calculated targets with data and stats
        """
        from app.services.indicators import (
            resample_ohlcv_to_timeframe,
            align_higher_timeframe_to_lower
        )

        results = []
        df = df.copy().sort_values('Date').reset_index(drop=True)

        for config in targets_config:
            target_type = config.get('type', '')
            target_timeframe = config.get('timeframe')
            use_multi_timeframe = False
            working_df = df

            # Check if we need to calculate on a different timeframe
            if target_timeframe and dataset_timeframe and target_timeframe != dataset_timeframe:
                try:
                    working_df = resample_ohlcv_to_timeframe(
                        df, target_timeframe, source_timeframe=dataset_timeframe
                    )
                    use_multi_timeframe = True
                    logger.info(f"Resampled to {target_timeframe} for {target_type} target: {len(df)} -> {len(working_df)} bars")
                except ValueError as e:
                    logger.warning(f"Cannot resample to {target_timeframe}: {e}. Using base timeframe.")
                    working_df = df

            # Determine timeframe for days-to-bars conversion
            effective_timeframe = target_timeframe if use_multi_timeframe else (dataset_timeframe or '1h')

            try:
                if target_type == 'price_based':
                    direction = config.get('direction')
                    profit_pct = config.get('profitPct')
                    max_dd = config.get('maxDrawdownPct')
                    time_value = config.get('timeBars')
                    time_unit = config.get('timeBarsUnit', 'bars')
                    if direction is None:
                        raise ValueError("price_based target requires 'direction' field")
                    if profit_pct is None:
                        raise ValueError("price_based target requires 'profitPct' field")
                    if max_dd is None:
                        raise ValueError("price_based target requires 'maxDrawdownPct' field")
                    if time_value is None:
                        raise ValueError("price_based target requires 'timeBars' field")
                    # Convert days to bars if needed
                    if time_unit == 'days':
                        bars = days_to_bars(time_value, effective_timeframe)
                        logger.info(f"Converted {time_value} days to {bars} bars for {effective_timeframe}")
                    else:
                        bars = time_value
                    series = self._calculate_single_target(working_df, profit_pct, max_dd, bars, direction)
                    unit_label = f"{time_value}d" if time_unit == 'days' else f"{bars}b"
                    col_name = f"price_{direction}_{profit_pct}pct_{max_dd}dd_{unit_label}"
                    category = 'binary_classification'

                elif target_type == 'directional':
                    direction = config.get('direction')
                    horizon_value = config.get('horizon')
                    horizon_unit = config.get('horizonUnit', 'bars')
                    if direction is None:
                        raise ValueError("directional target requires 'direction' field")
                    if horizon_value is None:
                        raise ValueError("directional target requires 'horizon' field")
                    # Convert days to bars if needed
                    if horizon_unit == 'days':
                        horizon = days_to_bars(horizon_value, effective_timeframe)
                        logger.info(f"Converted {horizon_value} days to {horizon} bars for {effective_timeframe}")
                    else:
                        horizon = horizon_value
                    series = self.calculate_directional(working_df, horizon, direction)
                    col_name = series.name
                    category = 'binary_classification'

                elif target_type == 'triple_barrier':
                    profit_pct = config.get('profitPct')
                    stop_pct = config.get('stopPct')
                    max_bars_value = config.get('maxBars')
                    max_bars_unit = config.get('maxBarsUnit', 'bars')
                    if profit_pct is None:
                        raise ValueError("triple_barrier target requires 'profitPct' field")
                    if stop_pct is None:
                        raise ValueError("triple_barrier target requires 'stopPct' field")
                    if max_bars_value is None:
                        raise ValueError("triple_barrier target requires 'maxBars' field")
                    # Convert days to bars if needed
                    if max_bars_unit == 'days':
                        max_bars = days_to_bars(max_bars_value, effective_timeframe)
                        logger.info(f"Converted {max_bars_value} days to {max_bars} bars for {effective_timeframe}")
                    else:
                        max_bars = max_bars_value
                    series = self.calculate_triple_barrier(working_df, profit_pct, stop_pct, max_bars)
                    col_name = series.name
                    category = 'multiclass_classification'

                elif target_type == 'trend_reversal':
                    indicator = config.get('indicator')
                    params = config.get('indicatorParams')
                    threshold = config.get('threshold')
                    direction = config.get('direction')
                    if indicator is None:
                        raise ValueError("trend_reversal target requires 'indicator' field")
                    if params is None:
                        raise ValueError("trend_reversal target requires 'indicatorParams' field")
                    if threshold is None:
                        raise ValueError("trend_reversal target requires 'threshold' field")
                    if direction is None:
                        raise ValueError("trend_reversal target requires 'direction' field")
                    series = self.calculate_trend_reversal(working_df, indicator, params, threshold, direction)
                    col_name = series.name
                    category = 'binary_classification'

                elif target_type == 'volatility':
                    horizon_value = config.get('horizon')
                    horizon_unit = config.get('horizonUnit', 'bars')
                    method = config.get('method')
                    if horizon_value is None:
                        raise ValueError("volatility target requires 'horizon' field")
                    if method is None:
                        raise ValueError("volatility target requires 'method' field")
                    # Convert days to bars if needed
                    if horizon_unit == 'days':
                        horizon = days_to_bars(horizon_value, effective_timeframe)
                        logger.info(f"Converted {horizon_value} days to {horizon} bars for {effective_timeframe}")
                    else:
                        horizon = horizon_value
                    series = self.calculate_volatility(working_df, horizon, method)
                    col_name = series.name
                    category = 'regression'

                else:
                    logger.warning(f"Unknown target type: {target_type}")
                    continue

                # If multi-timeframe, align result back to original timeframe
                if use_multi_timeframe:
                    # Build dataframe with the result for alignment
                    higher_tf_data = working_df[['Date']].copy()
                    higher_tf_data['_target'] = series.values

                    # Align back to original timeframe
                    aligned = align_higher_timeframe_to_lower(df, higher_tf_data, ['_target'])
                    series = pd.Series(aligned['_target'].values, index=df.index)

                    # Add timeframe suffix to column name
                    col_name = f"{col_name}_{target_timeframe}"
                    logger.info(f"Aligned {target_type} target from {target_timeframe} to base timeframe")

                # Calculate statistics
                valid_mask = ~pd.isna(series)
                valid_values = series[valid_mask]

                stats = {
                    "totalRows": len(series),
                    "validRows": int(valid_mask.sum())
                }

                if category in ['binary_classification']:
                    pos_count = int((valid_values == 1).sum())
                    neg_count = int((valid_values == 0).sum())
                    total = pos_count + neg_count
                    stats["positiveCount"] = pos_count
                    stats["negativeCount"] = neg_count
                    stats["positivePct"] = round(100 * pos_count / total, 2) if total > 0 else 0
                    stats["negativePct"] = round(100 * neg_count / total, 2) if total > 0 else 0

                elif category == 'multiclass_classification':
                    profit_count = int((valid_values == 1).sum())
                    stop_count = int((valid_values == 0).sum())
                    timeout_count = int((valid_values == 2).sum())
                    total = profit_count + stop_count + timeout_count
                    stats["profitHitCount"] = profit_count
                    stats["stopHitCount"] = stop_count
                    stats["timeoutCount"] = timeout_count
                    stats["profitHitPct"] = round(100 * profit_count / total, 2) if total > 0 else 0
                    stats["stopHitPct"] = round(100 * stop_count / total, 2) if total > 0 else 0
                    stats["timeoutPct"] = round(100 * timeout_count / total, 2) if total > 0 else 0

                elif category == 'regression':
                    if len(valid_values) > 0:
                        stats["mean"] = round(float(valid_values.mean()), 4)
                        stats["std"] = round(float(valid_values.std()), 4)
                        stats["min"] = round(float(valid_values.min()), 4)
                        stats["max"] = round(float(valid_values.max()), 4)

                # Prepare data for frontend
                # Use ISO format for dates to match preview endpoint (important for JS timestamp parsing)
                data = []
                for i in range(len(series)):
                    if 'Date' in df.columns:
                        date_val = df['Date'].iloc[i]
                        date_str = date_val.isoformat() if hasattr(date_val, 'isoformat') else str(date_val)
                    else:
                        date_str = str(i)
                    data.append({
                        "date": date_str,
                        "value": None if pd.isna(series.iloc[i]) else float(series.iloc[i])
                    })

                results.append({
                    "config": config,
                    "columnName": col_name,
                    "category": category,
                    "stats": stats,
                    "data": data
                })

            except Exception as e:
                logger.error(f"Error calculating {target_type} target: {e}", exc_info=True)
                results.append({
                    "config": config,
                    "columnName": f"error_{target_type}",
                    "category": "error",
                    "stats": {"error": str(e)},
                    "data": []
                })

        return results


class ClassImbalanceConfig:
    """
    Configuration for handling class imbalance in binary classification.

    Provides factory methods for getting loss functions and fitness metrics
    optimized for imbalanced datasets (common with prediction targets).
    """

    # Available loss functions
    LOSS_FUNCTIONS = {
        'cross_entropy': {
            'name': 'Cross-Entropy',
            'description': 'Standard cross-entropy loss. NOT recommended for imbalanced data.',
            'recommended_for': 'Balanced datasets only'
        },
        'weighted_cross_entropy': {
            'name': 'Weighted Cross-Entropy',
            'description': 'Cross-entropy with class weights based on frequency.',
            'recommended_for': 'Moderate imbalance (10-30% minority class)'
        },
        'focal_loss': {
            'name': 'Focal Loss',
            'description': 'Down-weights easy examples, focuses on hard ones. Best for severe imbalance.',
            'recommended_for': 'Severe imbalance (<10% minority class)',
            'default_gamma': 2.0
        }
    }

    # Available fitness metrics for genetic algorithm
    FITNESS_METRICS = {
        'accuracy': {
            'name': 'Accuracy',
            'description': 'Proportion of correct predictions. NOT recommended for imbalanced data.',
            'recommended_for': 'Balanced datasets only'
        },
        'f1_score': {
            'name': 'F1 Score',
            'description': 'Harmonic mean of precision and recall. Best general-purpose metric.',
            'recommended_for': 'Most imbalanced scenarios (DEFAULT)'
        },
        'precision': {
            'name': 'Precision',
            'description': 'Minimize false positives. Use when false alarms are costly.',
            'recommended_for': 'When avoiding false positives is critical'
        },
        'recall': {
            'name': 'Recall',
            'description': 'Minimize false negatives. Use when catching all positives is critical.',
            'recommended_for': 'When missing positive cases is critical'
        },
        'auc_roc': {
            'name': 'AUC-ROC',
            'description': 'Area under ROC curve. Threshold-independent metric.',
            'recommended_for': 'When threshold will be tuned later'
        },
        'balanced_accuracy': {
            'name': 'Balanced Accuracy',
            'description': 'Average of per-class recall. Simple balanced metric.',
            'recommended_for': 'Quick balanced evaluation'
        }
    }

    @staticmethod
    def get_loss_function(
        loss_type: str,
        positive_count: int = None,
        negative_count: int = None,
        gamma: float = 2.0,
        alpha: float = None
    ):
        """
        Get a PyTorch loss function for training.

        Args:
            loss_type: One of 'cross_entropy', 'weighted_cross_entropy', 'focal_loss'
            positive_count: Number of positive samples (for auto-weighting)
            negative_count: Number of negative samples (for auto-weighting)
            gamma: Focal loss gamma parameter (focusing strength)
            alpha: Optional alpha override for focal loss

        Returns:
            PyTorch loss module
        """
        from app.services.losses import get_loss_function
        return get_loss_function(loss_type, positive_count, negative_count, gamma, alpha)

    @staticmethod
    def get_recommended_config(positive_count: int, negative_count: int) -> dict:
        """
        Get recommended loss function and fitness metric based on class distribution.

        Args:
            positive_count: Number of positive samples
            negative_count: Number of negative samples

        Returns:
            Dictionary with recommended configuration
        """
        total = positive_count + negative_count
        positive_pct = (positive_count / total * 100) if total > 0 else 50

        if positive_pct < 5:
            # Extreme imbalance
            return {
                'loss_function': 'focal_loss',
                'gamma': 2.5,  # Higher gamma for extreme imbalance
                'fitness_metric': 'f1_score',
                'warning': f'Extreme class imbalance ({positive_pct:.1f}% positive). '
                          f'Model may struggle. Consider adjusting targets.'
            }
        elif positive_pct < 10:
            # Severe imbalance
            return {
                'loss_function': 'focal_loss',
                'gamma': 2.0,
                'fitness_metric': 'f1_score',
                'warning': f'Severe class imbalance ({positive_pct:.1f}% positive). '
                          f'Using Focal Loss with F1 metric.'
            }
        elif positive_pct < 30:
            # Moderate imbalance
            return {
                'loss_function': 'weighted_cross_entropy',
                'fitness_metric': 'f1_score',
                'warning': None
            }
        else:
            # Relatively balanced
            return {
                'loss_function': 'cross_entropy',
                'fitness_metric': 'accuracy',
                'warning': None
            }

    @staticmethod
    def get_available_configs() -> dict:
        """Get all available loss functions and fitness metrics."""
        return {
            'loss_functions': ClassImbalanceConfig.LOSS_FUNCTIONS,
            'fitness_metrics': ClassImbalanceConfig.FITNESS_METRICS,
            'defaults': {
                'loss_function': 'focal_loss',
                'fitness_metric': 'f1_score',
                'gamma': 2.0
            }
        }


class DatasetSplitter:
    """
    Service for splitting datasets into train/test sets.
    """

    @staticmethod
    def train_test_split(
        df: pd.DataFrame,
        train_ratio: float = 0.8,
        shuffle: bool = False
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Split dataset into train and test sets.

        Args:
            df: DataFrame to split
            train_ratio: Proportion of data for training (default: 0.8)
            shuffle: Whether to shuffle before splitting (default: False for timeseries)

        Returns:
            Tuple of (train_df, test_df)
        """
        df_sorted = df.sort_values('Date').reset_index(drop=True)

        if shuffle:
            df_sorted = df_sorted.sample(frac=1, random_state=42).reset_index(drop=True)

        split_idx = int(len(df_sorted) * train_ratio)

        train_df = df_sorted.iloc[:split_idx].copy()
        test_df = df_sorted.iloc[split_idx:].copy()

        logger.info(f"Split dataset: train={len(train_df)} rows, test={len(test_df)} rows")

        return train_df, test_df

    @staticmethod
    def walk_forward_split(
        df: pd.DataFrame,
        n_splits: int = 5,
        test_ratio: float = 0.2
    ) -> List[Tuple[pd.DataFrame, pd.DataFrame]]:
        """
        Create walk-forward validation splits for time series.

        Args:
            df: DataFrame to split
            n_splits: Number of train/test splits
            test_ratio: Proportion of data for each test set

        Returns:
            List of (train_df, test_df) tuples
        """
        df_sorted = df.sort_values('Date').reset_index(drop=True)
        n = len(df_sorted)

        splits = []
        test_size = int(n * test_ratio)

        for i in range(n_splits):
            # Growing training set
            train_end = int(n * (1 - test_ratio * (n_splits - i) / n_splits))
            test_end = min(train_end + test_size, n)

            train_df = df_sorted.iloc[:train_end].copy()
            test_df = df_sorted.iloc[train_end:test_end].copy()

            if len(test_df) > 0:
                splits.append((train_df, test_df))

        logger.info(f"Created {len(splits)} walk-forward splits")
        return splits
