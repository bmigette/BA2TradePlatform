"""Lightweight metric-coercion helpers shared by the expert backtest results builder
(``app.services.backtest.results``) and the legacy ML backtest handler
(``app.services.backtest_handler``).

WHY ITS OWN MODULE: these two pure helpers are deliberately kept here with NO heavy
imports (only ``numpy``) so the expert daily-backtest path

    daily_backtest_handler -> backtest.results -> metrics_utils

does NOT transitively import the ML training stack. ``app.services.backtest_handler``
top-imports ``tsai_training`` (-> tsai/torch/transformers) and, through it,
``darts_training`` (-> darts/pytorch-lightning/sklearn). That cost ~7s of process
startup for a backtest that never touches ML — paid again per genetic-optimizer worker
process. ``results.py`` previously imported these helpers FROM ``backtest_handler``,
which is what pulled the whole stack in; importing them from here removes that edge.
"""
from __future__ import annotations

import numpy as np


def _safe_float(value, default: float = 0.0) -> float:
    """Safely convert a value to float, handling NaN and Inf."""
    if value is None or (isinstance(value, float) and (np.isnan(value) or np.isinf(value))):
        return default
    try:
        result = float(value)
        if np.isnan(result) or np.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _safe_duration_days(duration, default: float = 0.0) -> float:
    """Safely extract days from a duration/timedelta."""
    if duration is None:
        return default
    if hasattr(duration, 'days'):
        return float(duration.days) + duration.seconds / 86400
    if hasattr(duration, 'total_seconds'):
        return duration.total_seconds() / 86400
    try:
        return float(duration)
    except (TypeError, ValueError):
        return default
