"""The three wizard targets of the custom ML models, on top of ``ml_targets`` (docs/plans/2026-10-27-custom-ml-models-design.md).

* ``triple_barrier_atr``: ``horizonSessions`` (default 5), ``slRatio`` (SL multiple / TP multiple, default 1.0),
  optional ``tpAtr`` (blank = fitted on the training rows so the timeout share is about a third);
* ``direction_terciles``: ``horizonSessions`` (default 3), thresholds fitted on the training rows;
* ``forward_return``: ``horizonSessions`` (default 5), the N-session forward return (regression).

They need the bar-end index and session structure of a dataset built from the ``alpaca_hist`` 1h / 15m source and
are refused on any other. Labels are computed on ONE symbol's frame, fitted on the rows the job's train block will use.

DAILY targets (ML exploration v2, Phase 7), for datasets of the ``alpaca_hist`` 1d source (the daily point-in-time
feature store): the decision is taken at session S's close and filled at S+1's OPEN, the outcome runs to the close of S+H
(``ml_targets.daily_targets``), H one of 1 / 3 / 5 / 10 / 20 sessions (``horizonSessions``, default 5):

* ``daily_up`` (binary 0 / 1): the return is positive. ``daily_multistep`` (``horizons``): one such binary column
  ``up_h<H>`` per horizon of the list (sorted ascending), the shortest one is the trained column and every column is purged by the longest;
* ``daily_volatility`` (regression, H >= 2): the std of the daily log returns of sessions S+1..S+H;
* ``daily_rel_return`` (regression), ``daily_quintile`` (1 = top fifth of the session, 0 = bottom fifth, the rest has
  no label) and ``daily_beats`` (the relative return is positive): compared with the other tickers of the SAME session,
  so they are computed across all the job's datasets (``build_cross_sectional_daily_targets``) and need at least 5.

The per-symbol ones are checked on the rows of the job's train block (``ml_targets.fit_daily_target``: a train block with a
single class is refused); nothing is fitted for the cross-sectional ones. The look-ahead of a label is its horizon in rows
(one row = one session).
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

from app.services import ml_targets as T
from app.services.ml_dataset_source import ALPACA_HIST_TIMEFRAMES, DAILY_TIMEFRAME

WIZARD_TARGET_TYPES = ("triple_barrier_atr", "direction_terciles", "forward_return")
_KIND = {"triple_barrier_atr": "triple_barrier", "direction_terciles": "direction", "forward_return": "forward_return"}
_DEFAULT_HORIZON = {"triple_barrier_atr": 5, "direction_terciles": 3, "forward_return": 5}


class WizardTargetError(ValueError):
    """A wizard target config is invalid or its dataset cannot carry it."""


DAILY_TARGET_TYPES = ("daily_rel_return", "daily_quintile", "daily_beats", "daily_up", "daily_volatility",
                      "daily_multistep")
#: compared with the other tickers of the same session: computed across the job's datasets
CROSS_SECTIONAL_DAILY_TYPES = ("daily_rel_return", "daily_quintile", "daily_beats")
ALL_WIZARD_TARGET_TYPES = WIZARD_TARGET_TYPES + DAILY_TARGET_TYPES
DAILY_HORIZON_CHOICES: Tuple[int, ...] = T.DAILY_HORIZONS
_DAILY_DEFAULT_HORIZON = 5
_DAILY_COLUMN = {"daily_rel_return": "rel_ret", "daily_quintile": "quintile", "daily_beats": "beats",
                 "daily_up": "up", "daily_volatility": "realvol", "daily_multistep": "up"}
#: the ``ml_targets.daily_targets`` kinds that build each type
_DAILY_KINDS = {"daily_rel_return": ["rel_ret"], "daily_quintile": ["quintile"], "daily_beats": ["beats"],
                "daily_up": ["ret", "up"], "daily_volatility": ["realvol"], "daily_multistep": ["ret", "up"]}


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


def _daily_horizon(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value \
            or int(value) not in DAILY_HORIZON_CHOICES:
        raise WizardTargetError(f"{what} horizon must be one of {list(DAILY_HORIZON_CHOICES)} sessions, got {value!r}")
    return int(value)


def daily_horizons(pt: Dict[str, Any]) -> List[int]:
    """The horizons (sessions) of a daily target config: ``horizons`` (a distinct, non-empty subset of the choices) for
    ``daily_multistep``, else the single ``horizonSessions`` (default 5; volatility needs at least 2)."""
    typ = pt["type"]
    if typ == "daily_multistep":
        hs = pt.get("horizons")
        if not isinstance(hs, (list, tuple)) or not hs:
            raise WizardTargetError(f"{typ} needs horizons, a non-empty list of {list(DAILY_HORIZON_CHOICES)} sessions")
        out = [_daily_horizon(h, typ) for h in hs]
        if len(set(out)) != len(out):
            raise WizardTargetError(f"{typ} horizons must be distinct, got {out}")
        return sorted(out)       # the shortest horizon is the trained column, whatever order the list came in
    h = pt.get("horizonSessions")
    h = _daily_horizon(_DAILY_DEFAULT_HORIZON if h is None else h, typ)
    if typ == "daily_volatility" and h < 2:
        raise WizardTargetError(f"{typ} needs a horizon of at least 2 sessions (one daily return has no spread), got {h}")
    return [h]


def column_names(pt: Dict[str, Any]) -> List[str]:
    """Columns of a wizard target, in the order they are trained on: one, except ``daily_multistep`` (one per horizon)."""
    if pt["type"] in DAILY_TARGET_TYPES:
        return [f"{_DAILY_COLUMN[pt['type']]}_h{h}" for h in daily_horizons(pt)]
    return [column_name(pt)]


def column_name(pt: Dict[str, Any]) -> str:
    """Column of a wizard target: ``tb_atr_h{H}_r{ratio}`` (+ ``_k{k}`` when fixed), ``dir_terc_h{H}``, ``fwd_ret_h{H}``;
    the daily ones ``rel_ret_h{H}``, ``quintile_h{H}``, ``beats_h{H}``, ``up_h{H}``, ``realvol_h{H}`` (a multistep
    target: the column of its first horizon, see ``column_names``)."""
    if pt["type"] in DAILY_TARGET_TYPES:
        return column_names(pt)[0]
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


# ------------------------------------------------------------------------------------------ daily targets
def _require_daily(typ: str, dataset_timeframe: str, panel_source: bool) -> None:
    if not panel_source or dataset_timeframe != DAILY_TIMEFRAME:
        raise WizardTargetError(
            f"{typ} needs a dataset built from the Alpaca history cache's daily point-in-time feature store (data "
            f"provider alpaca_hist, timeframe {DAILY_TIMEFRAME}); this dataset is {dataset_timeframe!r}"
            f"{'' if panel_source else ' from another data provider'}")


def _daily_bars(frame: pd.DataFrame, what: str) -> pd.DataFrame:
    """The ``ml_targets.daily_targets`` bars (open / high / low / close / listed / filled / ended, indexed by session) of
    one dataset frame. Every row of a daily dataset is a listed session."""
    for c in ("Date", "Open", "High", "Low", "Close", "filled"):
        if c not in frame.columns:
            raise WizardTargetError(
                f"{what}: the dataset has no {c!r} column; rebuild it from the alpaca_hist 1d source")
    if frame["filled"].dtype != bool:
        raise WizardTargetError(f"{what}: the 'filled' column must be boolean, got {frame['filled'].dtype}")
    dates = pd.DatetimeIndex(pd.to_datetime(frame["Date"]))
    if dates.tz is not None or (dates != dates.normalize()).any():
        raise WizardTargetError(f"{what}: Date must hold naive session dates (one bar per day)")
    if not dates.is_monotonic_increasing or not dates.is_unique:
        raise WizardTargetError(f"{what}: Date must be strictly increasing (one row per session)")
    px = frame[["Open", "High", "Low", "Close"]].astype(float)
    if px.isna().any().any():
        raise WizardTargetError(f"{what}: NaN in the daily bars")
    return pd.DataFrame({"open": px["Open"].to_numpy(), "high": px["High"].to_numpy(), "low": px["Low"].to_numpy(),
                         "close": px["Close"].to_numpy(), "listed": True, "filled": frame["filled"].to_numpy(),
                         "ended": False}, index=dates.rename("session"))


def build_daily_target(frame: pd.DataFrame, pt: Dict[str, Any], train_ratio: float, dataset_timeframe: str,
                       panel_source: bool) -> List[Tuple[pd.Series, str, int]]:
    """``[(labels aligned to frame, column name, look-ahead in rows)]`` of a PER-SYMBOL daily target (daily_up,
    daily_volatility, daily_multistep) on ONE dataset frame of the alpaca_hist 1d source (columns Date, Open, High, Low,
    Close, filled). Raises WizardTargetError for another source, an invalid config, a cross-sectional type or a train
    block that cannot carry the target."""
    typ = pt["type"]
    _require_daily(typ, dataset_timeframe, panel_source)
    if typ in CROSS_SECTIONAL_DAILY_TYPES:
        raise WizardTargetError(f"{typ} compares each ticker with the others of the same session: it is computed "
                                f"across the job's tickers (build_cross_sectional_daily_targets), not on one dataset")
    hs = daily_horizons(pt)
    cols = column_names(pt)
    if not 0.0 < train_ratio < 1.0:
        raise WizardTargetError(f"train ratio must be in (0, 1), got {train_ratio}")
    tg = T.daily_targets({"X": _daily_bars(frame, typ)}, hs, kinds=_DAILY_KINDS[typ])
    lookahead = max(hs)                  # a multistep target is purged by its longest horizon, whichever column trains
    out: List[Tuple[pd.Series, str, int]] = []
    for h, col in zip(hs, cols):
        labels = tg[f"{_DAILY_COLUMN[typ]}_{h}"].to_numpy(dtype=np.float64)
        observable = ~np.isnan(labels)
        if not observable.any():
            raise WizardTargetError(f"{col}: no row has an outcome ({len(frame)} sessions, horizon {h})")
        if typ != "daily_volatility":
            # nothing is fitted, but the train block must hold both outcomes (the same refusal as the exploration's)
            mask = train_block_mask(observable, train_ratio, lookahead)
            try:
                T.fit_daily_target(tg, T.TargetSpec("binary_direction", h), mask)
            except T.TargetBalanceError as e:
                raise WizardTargetError(str(e))
        out.append((pd.Series(labels, index=frame.index, name=col), col, lookahead))
    return out


def build_cross_sectional_daily_targets(frames: Sequence[pd.DataFrame], tickers: Sequence[str],
                                        prediction_targets: Sequence[Dict[str, Any]],
                                        dataset_timeframes: Sequence[str], panel_sources: Sequence[bool]
                                        ) -> List[Dict[str, Tuple[pd.Series, int]]]:
    """The cross-sectional daily targets (daily_rel_return, daily_quintile, daily_beats) of the job, per dataset: for
    each frame a ``{column: (labels aligned to the frame, look-ahead in rows)}`` dict (empty when the job asks for none).

    Every session's cross-section is the job's tickers listed on it (``ml_targets.daily_targets``): a session with fewer
    than ``MIN_SESSION_MEMBERS`` tickers holding an outcome has no label. Raises WizardTargetError when the job has
    fewer than that many datasets (a single ticker has nothing to be compared with), a ticker twice, a dataset from
    another source, or when no session has enough tickers."""
    pts = [pt for pt in prediction_targets if pt.get("type") in CROSS_SECTIONAL_DAILY_TYPES]
    results: List[Dict[str, Tuple[pd.Series, int]]] = [{} for _ in frames]
    if not pts:
        return results
    types = ", ".join(dict.fromkeys(pt["type"] for pt in pts))
    for tf, src in zip(dataset_timeframes, panel_sources):
        _require_daily(types, tf, src)
    n_min = T.MIN_SESSION_MEMBERS
    if len(frames) < n_min:
        raise WizardTargetError(
            f"{types} compare each ticker with the other tickers of the same session and need at least {n_min} datasets "
            f"(tickers) in the job, got {len(frames)}{' (a single ticker)' if len(frames) == 1 else ''}")
    if len(set(tickers)) != len(tickers):
        raise WizardTargetError(f"{types}: duplicate tickers in the job's datasets: {list(tickers)}")
    horizons = sorted({h for pt in pts for h in daily_horizons(pt)})
    kinds = sorted({k for pt in pts for k in _DAILY_KINDS[pt["type"]]})
    own = {tk: _daily_bars(f, f"{types} ({tk})") for tk, f in zip(tickers, frames)}
    sessions = pd.DatetimeIndex(sorted(set().union(*(b.index for b in own.values())))).rename("session")
    bars: Dict[str, pd.DataFrame] = {}
    for tk, b in own.items():
        d = b.drop(columns=["filled"]).reindex(sessions)
        d["listed"] = sessions.isin(b.index)             # a ticker is in the cross-section on its own sessions only
        d["filled"] = b["filled"].reindex(sessions, fill_value=False).astype(bool)
        d["ended"] = False
        bars[tk] = d
    tg = T.daily_targets(bars, horizons, kinds=kinds)
    for pt in pts:
        for h, col in zip(daily_horizons(pt), column_names(pt)):
            name = f"{_DAILY_COLUMN[pt['type']]}_{h}"
            if not tg[name].notna().any():
                raise WizardTargetError(
                    f"{col}: no session has {n_min} tickers with an outcome ({len(frames)} datasets, horizon {h}); "
                    f"the datasets must share their sessions")
            for i, (tk, f) in enumerate(zip(tickers, frames)):
                s = tg.xs(tk, level="symbol")[name].reindex(own[tk].index)
                results[i][col] = (pd.Series(s.to_numpy(dtype=np.float64), index=f.index, name=col), h)
    return results
