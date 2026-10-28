"""Neutral values for the cells the daily source's coverage mask blanked (shared by the darts and tsai training paths and by
prediction).

A dataset from the daily feature store marks a thin (group, year) with ``<prefix>missing`` = 1 and NaN in the group's columns
(``ml_features.apply_coverage_mask``). Those NaN are "no data", not "gap in a value": forward-filling them would show a model
last year's last value as this year's. Training fills each such cell with the MEDIAN of the column's unmasked values in the
train block (``fill_masked_covariates``), drops a group masked in every row, keeps the ``missing`` flag as a feature, and
SAVES the medians per ticker with the model (``MaskedFill`` / the ``masked_fill`` key of the saved normalization params).
Prediction fills from the saved medians and drops the saved columns (``apply_saved_fill``); it never refits on the frame it
predicts, and a model saved before this existed refuses a frame with masked cells (``StaleMaskedModel``).

Only the flags of the daily store's group prefixes count (``ml_dataset_source.daily_column_prefixes``), so any other dataset
passes through unchanged.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: key of the medians of a model trained on ONE dataset whose ticker was not recorded
SINGLE_KEY = "_single"
MASKED_FILL_VERSION = 1


class StaleMaskedModel(ValueError):
    """A model that cannot fill the masked cells of a frame: it was saved before masked-cell handling existed, or it never
    saw a masked value in that column. The remedy is to retrain."""


def masked_groups(cov: pd.DataFrame) -> List[Tuple[str, List[str], np.ndarray]]:
    """``(flag column, the group's columns, bool array of the flagged rows)`` for every daily-store group of ``cov`` that has
    at least one masked row (``<prefix>missing`` == 1). The ``_covered`` flags are never masked (they describe the data)."""
    from app.services.ml_dataset_source import daily_column_prefixes
    prefixes = set(daily_column_prefixes())
    found = []
    for flag in [c for c in cov.columns if c.endswith("_missing")]:
        prefix = flag[: -len("missing")]
        if prefix not in prefixes:
            continue
        flagged = (cov[flag] == 1.0).to_numpy()
        group = [c for c in cov.columns if c != flag and c.startswith(prefix)
                 and not c.endswith(("_covered", "_missing"))]
        if flagged.any() and group:
            found.append((flag, group, flagged))
    return found


def has_masked_cells(cov: pd.DataFrame) -> bool:
    """Some cell of a flagged group-year is still NaN (the coverage mask's blanks, not yet filled)."""
    return any(cov[group].isna().to_numpy()[flagged].any() for _, group, flagged in masked_groups(cov))


def all_masked_columns(frames: Sequence[pd.DataFrame], feature_columns: Sequence[str]) -> List[str]:
    """The feature columns of every group that is masked in EVERY row of ANY frame: the union across tickers, so every
    ticker keeps the same feature width. Their ``missing`` flags are not in the result."""
    cols = list(feature_columns)
    dropped: set = set()
    for df in frames:
        for _, group, flagged in masked_groups(df[cols]):
            if flagged.all():
                dropped |= set(group)
    return [c for c in cols if c in dropped]


def fill_masked_covariates(cov: pd.DataFrame, n_fit: int,
                           medians_out: Optional[Dict[str, float]] = None) -> Tuple[pd.DataFrame, List[str]]:
    """Neutral values for the cells the daily source's coverage mask blanked, so the past-only forward fill never carries
    a value across a masked (group, year).

    Each such cell becomes the MEDIAN of the column's unmasked values in the first ``n_fit`` rows (the train block, the same
    rows the covariate scaler is fitted on), so no later value reaches an earlier row. A median rather than 0 because the
    inputs are not standardised at this point (the scaler min-maxes them afterwards, so the median stays inside the train
    range); a column with no unmasked train value gets 0.0 and a warning. The ``missing`` flag itself is untouched and
    stays a feature. A group masked in every row has nothing to learn from: its columns are dropped (warning), its flag
    stays. ``medians_out`` (column -> value) receives the value used for every filled column.

    Returns ``(covariates, dropped columns)``."""
    out = cov.copy()
    dropped: List[str] = []
    for flag, group, flagged in masked_groups(cov):
        if flagged.all():
            logger.warning(f"Group {flag[:-len('missing')]!r} is masked in every row (coverage threshold): dropping its columns "
                           f"{group[:8]}{'...' if len(group) > 8 else ''}; {flag} stays as a feature")
            out = out.drop(columns=group)
            dropped += group
            continue
        for c in group:
            masked = flagged & out[c].isna().to_numpy()
            if not masked.any():
                continue
            train = out[c].iloc[:n_fit]
            train = train[~flagged[:n_fit]].dropna()
            if train.empty:
                logger.warning(f"{c}: no unmasked value in the train block, masked cells set to 0.0")
                neutral = 0.0
            else:
                neutral = float(train.median())
            out.loc[masked, c] = neutral
            if medians_out is not None:
                medians_out[c] = neutral
    return out, dropped


def ticker_key(df: pd.DataFrame) -> str:
    """The key of a training frame's medians: its ticker (multi-dataset jobs carry a ``ticker`` column), else ``SINGLE_KEY``."""
    if "ticker" in df.columns and len(df):
        return str(df["ticker"].iloc[0])
    return SINGLE_KEY


def build_masked_fill(per_ticker: Dict[str, Dict[str, float]], dropped: Sequence[str]) -> Dict[str, Any]:
    """The JSON saved with a model: the medians per ticker and per column, a ``pooled`` median per column (the median of the
    tickers' medians, for a ticker the model never trained on) and the all-masked columns that were dropped."""
    pooled: Dict[str, float] = {}
    for col in sorted({c for table in per_ticker.values() for c in table}):
        pooled[col] = float(np.median([table[col] for table in per_ticker.values() if col in table]))
    return {"version": MASKED_FILL_VERSION, "medians": {k: dict(v) for k, v in per_ticker.items()}, "pooled": pooled,
            "dropped_columns": list(dict.fromkeys(dropped))}


def _median_for(params: Dict[str, Any], key: Optional[str], column: str) -> float:
    medians = params["medians"]
    table = medians.get(key) if key is not None else None
    if table is None and SINGLE_KEY in medians and len(medians) == 1:
        table = medians[SINGLE_KEY]                 # trained on one dataset whose ticker was not recorded
    for source in (table, params["pooled"]):
        if source is not None and column in source:
            return float(source[column])
    raise StaleMaskedModel(f"retrain: this model saved no fill value for the masked column {column!r} "
                           f"(it never saw it masked in training)")


def apply_saved_fill(df: pd.DataFrame, feature_columns: Sequence[str], params: Optional[Dict[str, Any]],
                     ticker: Optional[str] = None) -> Tuple[pd.DataFrame, List[str]]:
    """``(df with its masked cells filled from the saved medians, the feature columns without the saved dropped ones)``.

    ``params`` is the model's saved ``masked_fill`` (``build_masked_fill``); None means the model was saved before
    masked-cell handling existed: a frame without masked cells passes through, one with them raises ``StaleMaskedModel``.
    The medians are the training ones: nothing is fitted on ``df``. A ``ticker`` column of ``df`` picks each row's
    ticker; otherwise ``ticker`` does; a ticker the model never trained on gets the pooled medians."""
    cols = list(feature_columns)
    if params is None:
        if has_masked_cells(df[cols]):
            raise StaleMaskedModel("retrain: this model predates masked-cell handling, and this frame has cells blanked "
                                   "by the coverage mask (<prefix>missing == 1)")
        return df, cols
    cols = [c for c in cols if c not in set(params["dropped_columns"])]
    out = df.copy()
    keys = out["ticker"].astype(str) if "ticker" in out.columns else None
    for _, group, flagged in masked_groups(out[cols]):
        for c in group:
            hole = flagged & out[c].isna().to_numpy()
            if not hole.any():
                continue
            if keys is None:
                out.loc[hole, c] = _median_for(params, ticker, c)
                continue
            for key in keys[hole].unique():
                out.loc[hole & (keys == key).to_numpy(), c] = _median_for(params, key, c)
    return out, cols
