"""
Chronos Foundation Model Service

Provides inference using Amazon Chronos-2 pre-trained time series models.
Handles automatic model download from HuggingFace, caching, and rolling
inference with signal conversion for backtesting integration.

Chronos-2 is a regression model that forecasts future values. This service
converts those forecasts into probability signals compatible with the
existing MLStrategy condition tree (model:prediction, model:probability).
"""

import logging
from typing import Dict, Any, Optional, List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Check for chronos availability
CHRONOS_AVAILABLE = False
try:
    from chronos import BaseChronosPipeline
    CHRONOS_AVAILABLE = True
    logger.info("chronos-forecasting library available")
except ImportError:
    logger.warning("chronos-forecasting not available. Install with: pip install chronos-forecasting")


# ============================================================================
# Model Registry
# ============================================================================

CHRONOS_MODELS = {
    'chronos-2': {
        'repo_id': 'amazon/chronos-2',
        'params': '120M',
        'description': 'Chronos-2 (120M) - multivariate + covariates',
        'supports_covariates': True,
        'max_context_length': 8192,
        'max_prediction_length': 1024,
    },
    'chronos-bolt-tiny': {
        'repo_id': 'amazon/chronos-bolt-tiny',
        'params': '9M',
        'description': 'Chronos-Bolt Tiny (9M) - fastest inference',
        'supports_covariates': False,
        'max_context_length': 2048,
        'max_prediction_length': 64,
    },
    'chronos-bolt-small': {
        'repo_id': 'amazon/chronos-bolt-small',
        'params': '48M',
        'description': 'Chronos-Bolt Small (48M) - fast inference',
        'supports_covariates': False,
        'max_context_length': 2048,
        'max_prediction_length': 64,
    },
    'chronos-bolt-base': {
        'repo_id': 'amazon/chronos-bolt-base',
        'params': '205M',
        'description': 'Chronos-Bolt Base (205M) - balanced speed/accuracy',
        'supports_covariates': False,
        'max_context_length': 2048,
        'max_prediction_length': 64,
    },
}

# Module-level pipeline cache to avoid reloading on every backtest
_pipeline_cache: Dict[str, Any] = {}


# ============================================================================
# Model Download + Loading
# ============================================================================

def get_pipeline(model_name: str = 'chronos-2'):
    """Load a Chronos pipeline, auto-downloading from HuggingFace if needed.

    Models are cached in ~/.cache/huggingface/hub/ by default (HuggingFace
    handles this automatically). Additionally, loaded pipelines are cached
    in memory so subsequent calls within the same process are instant.

    Args:
        model_name: Key from CHRONOS_MODELS registry

    Returns:
        A Chronos pipeline ready for inference

    Raises:
        RuntimeError: If chronos-forecasting is not installed
        ValueError: If model_name is not in the registry
    """
    if not CHRONOS_AVAILABLE:
        raise RuntimeError(
            "chronos-forecasting is not installed. "
            "Install with: pip install chronos-forecasting"
        )

    if model_name not in CHRONOS_MODELS:
        raise ValueError(
            f"Unknown Chronos model: {model_name}. "
            f"Available: {list(CHRONOS_MODELS.keys())}"
        )

    # Return cached pipeline if available
    if model_name in _pipeline_cache:
        logger.debug(f"Using cached Chronos pipeline: {model_name}")
        return _pipeline_cache[model_name]

    model_info = CHRONOS_MODELS[model_name]
    repo_id = model_info['repo_id']

    logger.info(f"Loading Chronos model '{model_name}' from {repo_id} (device=cpu)...")
    pipeline = BaseChronosPipeline.from_pretrained(
        repo_id,
        device_map="cpu",
    )
    logger.info(f"Chronos model '{model_name}' loaded successfully")

    _pipeline_cache[model_name] = pipeline
    return pipeline


def clear_pipeline_cache():
    """Clear the in-memory pipeline cache to free memory."""
    _pipeline_cache.clear()
    logger.info("Chronos pipeline cache cleared")


# ============================================================================
# Signal Conversion
# ============================================================================

def forecast_to_probabilities(
    forecast_median: float,
    current_price: float,
    scale_factor: float = 100.0,
) -> np.ndarray:
    """Convert a Chronos forecast into pseudo-probabilities [p_down, p_up].

    Uses a sigmoid function to map the predicted return to a probability.
    This makes the output compatible with the existing MLStrategy which
    expects class probabilities.

    Args:
        forecast_median: Median (q50) forecasted price
        current_price: Current price at the prediction point
        scale_factor: Controls how aggressively small returns map to
            high confidence. Higher = more confident signals.
            Default 100.0 means a 1% predicted return gives ~73% confidence.

    Returns:
        np.ndarray of shape (2,): [p_down, p_up]
    """
    if current_price == 0:
        return np.array([0.5, 0.5])

    predicted_return = (forecast_median - current_price) / current_price

    # Sigmoid: maps any real number to (0, 1)
    p_up = 1.0 / (1.0 + np.exp(-predicted_return * scale_factor))

    return np.array([1.0 - p_up, p_up])


# ============================================================================
# Rolling Inference
# ============================================================================

def run_chronos_inference(
    df: pd.DataFrame,
    prediction_length: int = 1,
    target_column: str = 'Close',
    model_name: str = 'chronos-2',
    min_context_length: int = 64,
    stride: int = 1,
    scale_factor: float = 100.0,
) -> Dict[pd.Timestamp, np.ndarray]:
    """Run rolling Chronos inference over a dataset.

    For each timestep t (after a minimum context window), feeds the
    price history to Chronos and converts the forecast into a
    probability signal compatible with MLStrategy.

    Instead of running inference one bar at a time (very slow), this
    uses batched inference: it creates multiple context windows and
    processes them together.

    Args:
        df: DataFrame with Date column and target_column
        prediction_length: How many steps ahead to forecast (default 1)
        target_column: Column to forecast (default 'Close')
        model_name: Chronos model variant to use
        min_context_length: Minimum history required before first prediction
        stride: Step size between predictions (1 = predict at every bar)
        scale_factor: Sigmoid scale for return-to-probability conversion

    Returns:
        Dict mapping timestamp -> probability array [p_down, p_up]
    """
    import torch

    if target_column not in df.columns:
        raise ValueError(f"Target column '{target_column}' not found in DataFrame")
    if 'Date' not in df.columns:
        raise ValueError("DataFrame must have a 'Date' column")

    pipeline = get_pipeline(model_name)
    model_info = CHRONOS_MODELS[model_name]
    max_context = model_info['max_context_length']

    prices = df[target_column].values.astype(np.float64)
    dates = pd.to_datetime(df['Date']).values
    n = len(prices)

    if n < min_context_length + prediction_length:
        raise ValueError(
            f"Dataset too short ({n} rows) for min_context_length={min_context_length} "
            f"+ prediction_length={prediction_length}"
        )

    # Build context windows for batched inference
    # Each window: prices[max(0, t-max_context):t] for t in range
    contexts = []
    prediction_indices = []

    for t in range(min_context_length, n, stride):
        start = max(0, t - max_context)
        context = prices[start:t]

        # Skip if context has NaN
        if np.any(np.isnan(context)):
            continue

        contexts.append(torch.tensor(context, dtype=torch.float32))
        prediction_indices.append(t)

    if not contexts:
        logger.warning("No valid context windows found (all contain NaN)")
        return {}

    logger.info(
        f"Chronos inference: {len(contexts)} predictions, "
        f"context lengths {len(contexts[0])}-{len(contexts[-1])}, "
        f"prediction_length={prediction_length}"
    )

    # Run batched inference
    # Chronos handles variable-length contexts by padding internally
    batch_size = 32
    predictions = {}

    for batch_start in range(0, len(contexts), batch_size):
        batch_end = min(batch_start + batch_size, len(contexts))
        batch_contexts = contexts[batch_start:batch_end]
        batch_indices = prediction_indices[batch_start:batch_end]

        with torch.no_grad():
            # predict() returns quantile forecasts: (batch, num_quantiles, prediction_length)
            forecast = pipeline.predict(
                batch_contexts,
                prediction_length=prediction_length,
            )

        # Extract median forecast (middle quantile)
        # forecast shape: (batch, num_quantiles, prediction_length)
        forecast_np = forecast.numpy()
        num_quantiles = forecast_np.shape[1]
        median_idx = num_quantiles // 2

        for i, t in enumerate(batch_indices):
            # Use first-step forecast median
            median_forecast = forecast_np[i, median_idx, 0]
            current_price = prices[t - 1]  # Price at prediction time

            probs = forecast_to_probabilities(
                median_forecast, current_price, scale_factor
            )

            ts = pd.Timestamp(dates[t])
            predictions[ts] = probs

    logger.info(
        f"Chronos inference complete: {len(predictions)} predictions generated"
    )
    return predictions


# ============================================================================
# Model Info / Status
# ============================================================================

def list_available_models() -> List[Dict[str, Any]]:
    """List all available Chronos model variants.

    Returns:
        List of dicts with model metadata
    """
    models = []
    for name, info in CHRONOS_MODELS.items():
        models.append({
            'name': name,
            'repo_id': info['repo_id'],
            'params': info['params'],
            'description': info['description'],
            'supports_covariates': info['supports_covariates'],
            'max_context_length': info['max_context_length'],
            'max_prediction_length': info['max_prediction_length'],
            'installed': CHRONOS_AVAILABLE,
        })
    return models


def get_model_info(model_name: str) -> Dict[str, Any]:
    """Get metadata for a specific Chronos model variant.

    Args:
        model_name: Key from CHRONOS_MODELS registry

    Returns:
        Dict with model metadata

    Raises:
        ValueError: If model_name is not in the registry
    """
    if model_name not in CHRONOS_MODELS:
        raise ValueError(
            f"Unknown Chronos model: {model_name}. "
            f"Available: {list(CHRONOS_MODELS.keys())}"
        )

    info = CHRONOS_MODELS[model_name].copy()
    info['name'] = model_name
    info['installed'] = CHRONOS_AVAILABLE
    info['cached'] = model_name in _pipeline_cache
    return info


def is_model_downloaded(model_name: str) -> bool:
    """Check if model weights are already cached locally in HuggingFace cache.

    Args:
        model_name: Key from CHRONOS_MODELS registry

    Returns:
        True if the model is cached locally
    """
    if model_name not in CHRONOS_MODELS:
        return False

    try:
        from huggingface_hub import try_to_load_from_cache
        repo_id = CHRONOS_MODELS[model_name]['repo_id']
        # Check for the config file as a proxy for "model is downloaded"
        result = try_to_load_from_cache(repo_id, "config.json")
        return result is not None
    except Exception:
        return False
