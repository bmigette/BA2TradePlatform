"""Neutral values for the cells the daily source's coverage mask blanked (shared by the darts and tsai training paths and by
prediction).

A dataset from the daily feature store marks a thin (group, year) with ``<prefix>missing`` = 1 and NaN in the group's columns
(``ml_features.apply_coverage_mask``). Those NaN are "no data", not "gap in a value": forward-filling them would show a model
last year's last value as this year's. Training fills each such cell with the MEDIAN of the column's unmasked values in the
train block (``fill_masked_covariates``), drops a group masked in every row and a column with no unmasked train value (never
a made-up 0), keeps the ``missing`` flag as a feature, and SAVES the medians per dataset with the model (``build_masked_fill``
/ the ``masked_fill`` key of the saved normalization params). Prediction fills from the saved medians and drops the saved
columns (``apply_saved_fill``); it never refits on the prediction frame, and a model saved before this existed refuses a
frame with masked cells (``StaleMaskedModel``).

Only the flags of the daily store's group prefixes count (``ml_dataset_source.daily_column_prefixes``), so any other dataset
passes through unchanged and without a copy.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: key of the medians of a model trained on ONE dataset whose ticker and id were not recorded
SINGLE_KEY = "_single"
MASKED_FILL_VERSION = 1
#: frame columns that identify a training dataset (the job adds them; neither is a feature)
TICKER_COLUMN = "ticker"
DATASET_COLUMN = "dataset_id"


class StaleMaskedModel(ValueError):
    """A model that cannot fill the masked cells of a frame: it was saved before masked-cell handling existed, or it never
    saw a masked value in that column. The remedy is to retrain."""


def masked_groups(cov: pd.DataFrame) -> List[Tuple[str, List[str], np.ndarray]]:
    """``(flag column, the group's columns, bool array of the flagged rows)`` for every daily-store group of ``cov`` that has
    at least one masked row (``<prefix>missing`` == 1). The ``_covered`` flags are never masked (they describe the data)."""
    flags = [c for c in cov.columns if c.endswith("_missing")]
    if not flags:
        return []
    from app.services.ml_dataset_source import daily_column_prefixes
    prefixes = set(daily_column_prefixes())
    found = []
    for flag in flags:
        prefix = flag[: -len("missing")]
        if prefix not in prefixes:
            continue
        flagged = (cov[flag] == 1.0).to_numpy()
        group = [c for c in cov.columns if c != flag and c.startswith(prefix)
                 and not c.endswith(("_covered", "_missing"))]
        if flagged.any() and group:
            found.append((flag, group, flagged))
    return found


def has_daily_flags(columns: Sequence[str]) -> bool:
    """Some column is a ``<prefix>missing`` flag of the daily store (the dataset comes from the daily source)."""
    flags = [c for c in columns if c.endswith("_missing")]
    if not flags:
        return False
    from app.services.ml_dataset_source import daily_column_prefixes
    prefixes = set(daily_column_prefixes())
    return any(c[: -len("missing")] in prefixes for c in flags)


def has_masked_cells(cov: pd.DataFrame) -> bool:
    """Some cell of a flagged group-year is still NaN (the coverage mask's blanks, not yet filled)."""
    return any(cov[group].isna().to_numpy()[flagged].any() for _, group, flagged in masked_groups(cov))


def all_masked_columns(frames: Sequence[pd.DataFrame], feature_columns: Sequence[str]) -> List[str]:
    """The feature columns of every group that is masked in EVERY row of ANY frame: the union across tickers, so every
    ticker keeps the same feature width. Their ``missing`` flags are not in the result."""
    cols = list(feature_columns)
    dropped: set = set()
    for df in frames:
        # frames of a job need not share every column (a provider that lacks one): look at the ones this frame has
        for _, group, flagged in masked_groups(df[[c for c in cols if c in df.columns]]):
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
    range). The ``missing`` flag itself is untouched and stays a feature. Two cases have nothing to learn from and are
    DROPPED (logged), never filled with an invented value: a group masked in every row, and a column with no unmasked value
    in the train block. ``medians_out`` (column -> value) receives the value used for every filled column.

    A frame without a masked group is returned as it is (the same object, no copy). Returns ``(covariates, dropped)``."""
    groups = masked_groups(cov)
    if not groups:
        return cov, []
    out = cov.copy()
    dropped: List[str] = []
    for flag, group, flagged in groups:
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
                logger.warning(f"{c}: no unmasked value in the train block to take a median of: dropping the column "
                               f"({flag} stays as a feature)")
                out = out.drop(columns=[c])
                dropped.append(c)
                continue
            neutral = float(train.median())
            out.loc[masked, c] = neutral
            if medians_out is not None:
                medians_out[c] = neutral
    return out, dropped


def fill_key(ticker: Optional[str], dataset_id: Optional[Any]) -> str:
    """The key of a training dataset's medians: ``<ticker>#<dataset id>``, so two datasets of one ticker (other date ranges)
    do not collide. ``SINGLE_KEY`` when neither is known."""
    if ticker is None and dataset_id is None:
        return SINGLE_KEY
    if dataset_id is not None and not isinstance(dataset_id, str) and float(dataset_id).is_integer():
        dataset_id = int(dataset_id)
    return f"{'' if ticker is None else ticker}#{'' if dataset_id is None else dataset_id}"


def ticker_key(df: pd.DataFrame) -> str:
    """The ``fill_key`` of a training frame, from its ``ticker`` / ``dataset_id`` columns (the job adds them)."""
    if not len(df):
        return SINGLE_KEY
    t = str(df[TICKER_COLUMN].iloc[0]) if TICKER_COLUMN in df.columns else None
    d = df[DATASET_COLUMN].iloc[0] if DATASET_COLUMN in df.columns else None
    return fill_key(t, d)


def build_masked_fill(per_dataset: Dict[str, Dict[str, float]], dropped: Sequence[str]) -> Dict[str, Any]:
    """The JSON saved with a model: the medians per training dataset (``fill_key``) and per column, a ``pooled`` median per
    column (the median of the datasets' medians, for a ticker the model never trained on) and the columns that were
    dropped (``dropped_masked_columns``: not the scaler's zero-variance ``dropped_columns``)."""
    pooled: Dict[str, float] = {}
    for col in sorted({c for table in per_dataset.values() for c in table}):
        pooled[col] = float(np.median([table[col] for table in per_dataset.values() if col in table]))
    return {"version": MASKED_FILL_VERSION, "medians": {k: dict(v) for k, v in per_dataset.items()}, "pooled": pooled,
            "dropped_masked_columns": list(dict.fromkeys(dropped))}


def _table_for(params: Dict[str, Any], ticker: Optional[str], dataset_id: Optional[Any]) -> Optional[Dict[str, float]]:
    """The saved medians of the dataset a frame belongs to: the exact ``ticker#id``, else the one trained on this dataset
    id, else the only training dataset of this ticker (or the single unnamed one); None when it is ambiguous."""
    medians = params["medians"]
    exact = fill_key(ticker, dataset_id)
    if exact in medians:
        return medians[exact]
    if dataset_id is not None:
        same_id = [k for k in medians if k.endswith(f"#{dataset_id}")]
        if len(same_id) == 1:
            return medians[same_id[0]]
    if ticker is not None:
        same_ticker = [k for k in medians if k.startswith(f"{ticker}#")]
        if len(same_ticker) == 1:
            return medians[same_ticker[0]]
    if len(medians) == 1 and SINGLE_KEY in medians:
        return medians[SINGLE_KEY]
    return None


def _median_for(params: Dict[str, Any], ticker: Optional[str], dataset_id: Optional[Any], column: str) -> float:
    table = _table_for(params, ticker, dataset_id)
    if table is not None and column in table:
        return float(table[column])
    if column in params["pooled"]:
        logger.warning(f"{column}: no saved median for {fill_key(ticker, dataset_id)!r} (a ticker or dataset the model did "
                       f"not train on, or ambiguous): using the pooled median of the training datasets")
        return float(params["pooled"][column])
    raise StaleMaskedModel(f"retrain: this model saved no fill value for the masked column {column!r} "
                           f"(it never saw it masked in training)")


def apply_saved_fill(df: pd.DataFrame, feature_columns: Sequence[str], params: Optional[Dict[str, Any]],
                     ticker: Optional[str] = None, dataset_id: Optional[Any] = None) -> Tuple[pd.DataFrame, List[str]]:
    """``(df with its masked cells filled from the saved medians, the feature columns without the saved dropped ones)``.

    ``params`` is the model's saved ``masked_fill`` (``build_masked_fill``); None means the model was saved before
    masked-cell handling existed: a frame without masked cells passes through, one with them raises ``StaleMaskedModel``.
    The medians are the training ones: nothing is fitted on ``df``. ``ticker`` / ``dataset_id`` (or the frame's own
    ``ticker`` / ``dataset_id`` columns, row by row) pick the training dataset's medians; one the model never trained on
    gets the pooled medians (warning). Columns the frame does not have are skipped."""
    cols = [c for c in feature_columns if c in df.columns]          # a frame need not have every column of the model
    if params is None:
        if has_masked_cells(df[cols]):
            raise StaleMaskedModel("retrain: this model predates masked-cell handling, and this frame has cells blanked "
                                   "by the coverage mask (<prefix>missing == 1)")
        return df, cols
    # the columns the model dropped; an earlier save called the key dropped_columns (no scaler params were merged into it)
    saved_drop = params["dropped_masked_columns"] if "dropped_masked_columns" in params else params.get("dropped_columns", [])
    cols = [c for c in cols if c not in set(saved_drop)]
    groups = masked_groups(df[cols])
    if not groups:
        return df, cols
    out = df.copy()
    tickers = out[TICKER_COLUMN].astype(str) if TICKER_COLUMN in out.columns else None
    ids = out[DATASET_COLUMN] if DATASET_COLUMN in out.columns else None
    for _, group, flagged in groups:
        for c in group:
            hole = flagged & out[c].isna().to_numpy()
            if not hole.any():
                continue
            if tickers is None and ids is None:
                out.loc[hole, c] = _median_for(params, ticker, dataset_id, c)
                continue
            pairs = pd.DataFrame({"t": tickers if tickers is not None else ticker,
                                  "d": ids if ids is not None else dataset_id})[hole].drop_duplicates()
            for t, d in pairs.itertuples(index=False):
                sel = hole.copy()
                if tickers is not None:
                    sel &= (tickers == t).to_numpy()
                if ids is not None:
                    sel &= (ids == d).to_numpy()
                out.loc[sel, c] = _median_for(params, None if pd.isna(t) else t, None if pd.isna(d) else d, c)
    return out, cols
