"""The three wizard targets of the custom ML models, on top of ``ml_targets`` (docs/plans/2026-10-27-custom-ml-models-design.md).

* ``triple_barrier_atr``: ``horizonSessions`` (default 5), ``slRatio`` (SL multiple / TP multiple, default 1.0),
  optional ``tpAtr`` (blank = fitted on the training rows so the timeout share is about a third);
* ``direction_terciles``: ``horizonSessions`` (default 3), thresholds fitted on the training rows;
* ``forward_return``: ``horizonSessions`` (default 5), the N-session forward return (regression).

They need the bar-end index and session structure of a dataset built from the ``alpaca_hist`` 1h / 15m source and
are refused on any other. Labels are computed on ONE symbol's frame, fitted on the rows the job's train block will use.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd

from app.services import ml_targets as T
from app.services.ml_dataset_source import ALPACA_HIST_TIMEFRAMES

WIZARD_TARGET_TYPES = ("triple_barrier_atr", "direction_terciles", "forward_return")
_KIND = {"triple_barrier_atr": "triple_barrier", "direction_terciles": "direction", "forward_return": "forward_return"}
_DEFAULT_HORIZON = {"triple_barrier_atr": 5, "direction_terciles": 3, "forward_return": 5}


class WizardTargetError(ValueError):
    """A wizard target config is invalid or its dataset cannot carry it."""


def _horizon(pt: Dict[str, Any]) -> int:
    h = pt.get("horizonSessions")
    h = _DEFAULT_HORIZON[pt["type"]] if h is None else h
    if int(h) != h or h <= 0:
        raise WizardTargetError(f"{pt['type']} horizonSessions must be a positive whole number, got {h!r}")
    return int(h)


def _fixed_k(pt: Dict[str, Any]):
    if pt["type"] != "triple_barrier_atr" or pt.get("tpAtr") in (None, ""):
        return None
    k = float(pt["tpAtr"])
    if not k > 0:
        raise WizardTargetError(f"triple_barrier_atr tpAtr must be > 0, got {pt['tpAtr']!r}")
    return k


def target_spec(pt: Dict[str, Any]) -> Tuple[T.TargetSpec, Any]:
    """(TargetSpec, fixed take-profit multiple or None) of a wizard target config."""
    ratio = pt.get("slRatio")
    try:
        spec = T.TargetSpec(_KIND[pt["type"]], _horizon(pt), 1.0 if ratio is None else float(ratio))
    except ValueError as e:
        raise WizardTargetError(str(e))
    return spec, _fixed_k(pt)


def column_name(pt: Dict[str, Any]) -> str:
    """Column of a wizard target: ``tb_atr_h{H}_r{ratio}`` (+ ``_k{k}`` when fixed), ``dir_terc_h{H}``, ``fwd_ret_h{H}``."""
    spec, k = target_spec(pt)
    if pt["type"] == "triple_barrier_atr":
        return f"tb_atr_h{spec.horizon}_r{spec.ratio:g}" + (f"_k{k:g}" if k is not None else "")
    if pt["type"] == "direction_terciles":
        return f"dir_terc_h{spec.horizon}"
    return f"fwd_ret_h{spec.horizon}"


def train_block_mask(observable: np.ndarray, train_ratio: float, lookahead: int) -> np.ndarray:
    """Rows of the job's train block, as ``split_frames_three_way`` cuts it: the frame is first reduced to its
    labelled rows (``observable``), the first ``train_ratio`` of those is the train side of the split and the last
    ``lookahead`` rows of that are purged."""
    from app.services.data_preparation import purged_train_row_count
    if not 0.0 < train_ratio < 1.0:
        raise WizardTargetError(f"train ratio must be in (0, 1), got {train_ratio}")
    pos = np.flatnonzero(observable)
    n_train = purged_train_row_count(int(len(pos) * train_ratio), lookahead)
    if n_train <= 0:
        raise WizardTargetError(f"{len(pos)} labelled rows are too few for a train block (look-ahead {lookahead} bars)")
    mask = np.zeros(len(observable), dtype=bool)
    mask[pos[:n_train]] = True
    return mask


def build_wizard_target(frame: pd.DataFrame, pt: Dict[str, Any], train_ratio: float, dataset_timeframe: str,
                        panel_source: bool) -> Tuple[pd.Series, str, int]:
    """(labels aligned to ``frame``, column name, look-ahead in bars) of one wizard target on ONE symbol's frame
    (columns Date, Open, High, Low, Close). Raises WizardTargetError unless the dataset is an alpaca_hist 1h / 15m one."""
    if not panel_source or dataset_timeframe not in ALPACA_HIST_TIMEFRAMES:
        raise WizardTargetError(
            f"{pt['type']} needs a dataset built from the Alpaca history cache (data provider alpaca_hist, timeframe "
            f"{' or '.join(ALPACA_HIST_TIMEFRAMES)}); this dataset is {dataset_timeframe!r}"
            f"{'' if panel_source else ' from another data provider'}")
    spec, k = target_spec(pt)
    col = column_name(pt)
    f = frame.set_index(pd.DatetimeIndex(pd.to_datetime(frame["Date"])))
    # pass 1 finds which rows can be labelled and the look-ahead (neither depends on the fitted thresholds);
    # pass 2 fits k / the terciles on the rows the split will really train on
    first, _, lookahead = T.labels_for_frame(f, spec, np.ones(len(f), dtype=bool), k=1.0 if k is None else k)
    if spec.kind == "forward_return" or k is not None:       # nothing is fitted: the labels do not depend on the mask
        return pd.Series(first.values, index=frame.index, name=col), col, lookahead
    mask = train_block_mask(first.notna().values, train_ratio, lookahead)
    labels, _, lookahead = T.labels_for_frame(f, spec, mask, k=k)
    return pd.Series(labels.values, index=frame.index, name=col), col, lookahead
