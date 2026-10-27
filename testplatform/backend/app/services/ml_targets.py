"""Targets and rolling folds for the custom ML models (docs/plans/2026-10-27-custom-ml-models-design.md, sections 1-2).

Every target uses three ordered classes, so one store row format serves all of them:
``0`` = down / stop first, ``1`` = flat / timeout, ``2`` = up / take-profit first.

* ``triple_barrier``: from the bar's close, does the price reach close + k x ATR (win) before close - m x ATR (loss)
  within N sessions (else timeout)? ATR is the daily ATR14 of the sessions BEFORE the bar (``atr_d``). A bar that
  touches both barriers counts as a loss (the conservative reading of an unknown intrabar order). ``m = ratio x k``;
  k is chosen on the TRAINING fold so the timeout share is closest to one third.
* ``direction``: the N-session forward return against the training fold's terciles.
* ``forward_return``: the N-session forward return itself (regression; the quantile models use it).
* ``binary_direction``: up (N-session forward return > 0) vs down. ``binary_barrier``: the take-profit reached first
  vs not (stop first OR timeout), with the triple barrier's k / m.
* ``regression``: the N-session forward return in volatility units (/ (daily ATR% x sqrt(N))), clipped at +-5; a
  point-forecast model (MSE), turned into deciles with the training fold's validation residuals.

Thresholds (k, terciles) are fitted on the training fold only. A fold whose smallest class share is below
``MIN_CLASS_SHARE`` is refused (``TargetBalanceError``), never rebalanced.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

TARGET_KINDS: Tuple[str, ...] = ("triple_barrier", "direction", "forward_return", "binary_direction",
                                 "binary_barrier", "regression")
#: two-class targets: labels 0 (down / not the take-profit) and 2 (up / take-profit first); class 1 is never used
BINARY_KINDS: Tuple[str, ...] = ("binary_direction", "binary_barrier")
MIN_CLASS_SHARE = 0.15
K_GRID: Tuple[float, ...] = tuple(np.round(np.arange(0.25, 6.01, 0.25), 2))
CLASS_NAMES = {"triple_barrier": ("loss", "timeout", "win"), "direction": ("down", "flat", "up"),
               "binary_direction": ("down", "-", "up"), "binary_barrier": ("no take-profit", "-", "take-profit first")}
#: regression target clip, in volatility units (the N-session return / (daily ATR% x sqrt(N)))
REGRESSION_CLIP = 5.0


class TargetBalanceError(ValueError):
    """A fold's smallest class share is below MIN_CLASS_SHARE."""


@dataclass(frozen=True)
class TargetSpec:
    kind: str
    horizon: int                 # sessions
    ratio: float = 1.0           # triple barrier: m / k

    def __post_init__(self):
        if self.kind not in TARGET_KINDS:
            raise ValueError(f"target kind must be one of {TARGET_KINDS}, got {self.kind!r}")
        if self.horizon <= 0:
            raise ValueError("horizon must be > 0")
        if self.kind in ("triple_barrier", "binary_barrier") and not self.ratio > 0:
            raise ValueError(f"{self.kind} ratio must be > 0")

    @property
    def is_classification(self) -> bool:
        return self.kind not in ("forward_return", "regression")

    @property
    def is_binary(self) -> bool:
        return self.kind in BINARY_KINDS

    @property
    def uses_darts(self) -> bool:
        return self.kind == "forward_return"


# ------------------------------------------------------------------ folds
@dataclass(frozen=True)
class Fold:
    name: str
    train_start: date
    pred_start: date
    pred_end: date               # inclusive

    def to_dict(self) -> Dict[str, str]:
        return {k: str(v) for k, v in asdict(self).items()}


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


def quarterly_folds(first_pred: date = date(2025, 1, 1), last_day: date = date(2026, 10, 12),
                    train_months: int = 12) -> List[Fold]:
    """Quarterly prediction blocks from ``first_pred`` to ``last_day``; each trains on the ``train_months`` before
    it. A trailing partial quarter shorter than half a quarter is merged into the previous block (2026-10-01..12
    joins 2026Q3, giving the design's 7 folds)."""
    out, start = [], first_pred
    while start <= last_day:
        nxt = _add_months(start, 3)
        end = min(last_day, nxt - pd.Timedelta(days=1).to_pytimedelta())
        if out and start > first_pred and (end - start).days < 45 and end == last_day:
            prev = out.pop()
            out.append(Fold(prev.name, prev.train_start, prev.pred_start, end))
            break
        out.append(Fold(f"{start.year}Q{(start.month - 1) // 3 + 1}", _add_months(start, -train_months), start, end))
        start = nxt
    return out


def fold_rows(panel: pd.DataFrame, fold: Fold, horizon: int) -> Tuple[np.ndarray, np.ndarray]:
    """(train, pred) row positions in ``panel``. A training row's target bar must lie in a session BEFORE the
    prediction block (the purge gap of one horizon); prediction rows are every bar in the block."""
    sess = pd.to_datetime(panel["session"]).dt.date.values
    tgt = panel[f"tgt_idx_{horizon}"].values
    tgt_sess = _target_sessions(panel, horizon)
    train = (sess >= fold.train_start) & (sess < fold.pred_start) & (tgt >= 0) & (tgt_sess < fold.pred_start)
    pred = (sess >= fold.pred_start) & (sess <= fold.pred_end)
    return np.flatnonzero(train), np.flatnonzero(pred)


def _target_sessions(panel: pd.DataFrame, horizon: int) -> np.ndarray:
    """Session of each row's target bar (date.max where there is none)."""
    out = np.full(len(panel), date.max, dtype=object)
    sess = pd.to_datetime(panel["session"]).dt.date.values
    for sym, g in panel.groupby("symbol", sort=False):
        pos = g.index.values
        tgt = g[f"tgt_idx_{horizon}"].values
        ok = tgt >= 0
        out[pos[ok]] = sess[pos[tgt[ok]]]
    return out


def split_validation(panel: pd.DataFrame, train_rows: np.ndarray, horizon: int, share: float = 0.15
                     ) -> Tuple[np.ndarray, np.ndarray]:
    """Time-ordered early-stopping split of the training rows: the last ``share`` of sessions validate; fitting rows
    whose target lands in or after the validation block are purged."""
    sess = pd.to_datetime(panel["session"]).dt.date.values
    ts = np.unique(sess[train_rows])
    vstart = ts[int(len(ts) * (1.0 - share))]
    tgt_sess = _target_sessions(panel, horizon)
    fit = train_rows[(sess[train_rows] < vstart) & (tgt_sess[train_rows] < vstart)]
    val = train_rows[sess[train_rows] >= vstart]
    return fit, val


# ------------------------------------------------------------------ triple barrier
def barrier_excursions(panel: pd.DataFrame, horizon: int) -> Tuple[np.ndarray, np.ndarray]:
    """Per row, the running max excursion above and below the row's close over the bars after it up to its target
    bar, in units of ``atr_d``: ``up[i, j] = (max high of bars i+1..i+1+j - close_i) / atr_i``. NaN-padded."""
    n = len(panel)
    steps = int((panel[f"tgt_idx_{horizon}"] - panel["row_in_symbol"]).where(panel[f"tgt_idx_{horizon}"] >= 0).max())
    up = np.full((n, steps), np.nan, dtype=np.float32)
    dn = np.full((n, steps), np.nan, dtype=np.float32)
    for sym, g in panel.groupby("symbol", sort=False):
        pos = g.index.values
        hi, lo, cl = g["high"].values, g["low"].values, g["close"].values
        atr = g["atr_d"].values
        tgt = g[f"tgt_idx_{horizon}"].values
        r = g["row_in_symbol"].values
        for i in range(len(g)):
            t = tgt[i]
            if t < 0 or not np.isfinite(atr[i]) or atr[i] <= 0:
                continue
            L = t - r[i]
            seg_h, seg_l = hi[i + 1:i + 1 + L], lo[i + 1:i + 1 + L]
            up[pos[i], :L] = (np.maximum.accumulate(seg_h) - cl[i]) / atr[i]
            dn[pos[i], :L] = (cl[i] - np.minimum.accumulate(seg_l)) / atr[i]
    return up, dn


def triple_barrier_labels(up: np.ndarray, dn: np.ndarray, k: float, m: float) -> np.ndarray:
    """0 loss / 1 timeout / 2 win, NaN where the row has no complete path."""
    valid = ~np.isnan(up[:, 0])
    # rows with a shorter path than the matrix width are NaN-padded at the end; only the last valid step matters
    hit_up, hit_dn = up >= k, dn >= m
    big = up.shape[1] + 1
    first_up = np.where(hit_up.any(1), hit_up.argmax(1), big)
    first_dn = np.where(hit_dn.any(1), hit_dn.argmax(1), big)
    lab = np.where(first_dn <= first_up, np.where(first_dn < big, 0, 1), 2).astype(np.float64)
    lab[~valid] = np.nan
    return lab


def choose_k(up: np.ndarray, dn: np.ndarray, ratio: float, rows: np.ndarray, target_timeout: float = 1 / 3) -> float:
    best, best_err = None, None
    for k in K_GRID:
        lab = triple_barrier_labels(up[rows], dn[rows], float(k), float(k) * ratio)
        lab = lab[~np.isnan(lab)]
        if not len(lab):
            continue
        err = abs((lab == 1).mean() - target_timeout)
        if best_err is None or err < best_err - 1e-12:
            best, best_err = float(k), err
    if best is None:
        raise TargetBalanceError("no training row has a complete barrier path")
    return best


# ------------------------------------------------------------------ fitted target per fold
@dataclass
class FoldTarget:
    """A target fitted on one fold's training rows: labels for every panel row and the per-class return values the
    store row carries (fraction of the entry price)."""
    spec: TargetSpec
    labels: np.ndarray                    # float, NaN = no label; regression: the forward return
    params: Dict[str, float]
    class_returns: Optional[np.ndarray]   # (n_rows, 3): down / flat / up return of each row, classification only


def fit_target(panel: pd.DataFrame, spec: TargetSpec, train_rows: np.ndarray,
               excursions: Optional[Tuple[np.ndarray, np.ndarray]] = None) -> FoldTarget:
    fwd = panel[f"fwd_ret_{spec.horizon}"].values.astype(np.float64)
    if spec.kind == "forward_return":
        return FoldTarget(spec, fwd, {}, None)
    if spec.kind == "regression":
        vol = (panel["atr_d"] / panel["close"]).values * np.sqrt(spec.horizon)
        z = np.clip(fwd / vol, -REGRESSION_CLIP, REGRESSION_CLIP)
        z[~np.isfinite(fwd) | ~np.isfinite(vol)] = np.nan
        return FoldTarget(spec, z, {"vol_unit": "daily ATR% x sqrt(N)", "clip": REGRESSION_CLIP}, None)
    if spec.kind == "binary_direction":
        tr = fwd[train_rows]
        tr = tr[np.isfinite(tr)]
        lab = np.where(fwd > 0, 2.0, 0.0)
        lab[~np.isfinite(fwd)] = np.nan
        means = [float(tr[tr <= 0].mean()), 0.0, float(tr[tr > 0].mean())]
        return FoldTarget(spec, lab, {}, np.tile(np.array(means), (len(panel), 1)))
    if spec.kind == "direction":
        tr = fwd[train_rows]
        tr = tr[np.isfinite(tr)]
        lo, hi = np.quantile(tr, [1 / 3, 2 / 3])
        lab = np.where(fwd <= lo, 0.0, np.where(fwd >= hi, 2.0, 1.0))
        lab[~np.isfinite(fwd)] = np.nan
        means = [float(tr[tr <= lo].mean()), float(tr[(tr > lo) & (tr < hi)].mean()), float(tr[tr >= hi].mean())]
        cr = np.tile(np.array(means), (len(panel), 1))
        return FoldTarget(spec, lab, {"lo": float(lo), "hi": float(hi)}, cr)
    up, dn = excursions if excursions is not None else barrier_excursions(panel, spec.horizon)
    k = choose_k(up, dn, spec.ratio, train_rows)
    m = k * spec.ratio
    lab = triple_barrier_labels(up, dn, k, m)
    atr_frac = (panel["atr_d"] / panel["close"]).values
    tl = lab[train_rows]
    tf = fwd[train_rows]
    if spec.kind == "binary_barrier":
        # "no take-profit" = stop first or timeout: its return value is the training fold's mean N-session return
        # of those rows (an expected value, not a stop level)
        not_win = float(np.nanmean(tf[(tl == 0) | (tl == 1)]))
        blab = np.where(lab == 2, 2.0, 0.0)
        blab[np.isnan(lab)] = np.nan
        cr = np.stack([np.full(len(panel), not_win), np.zeros(len(panel)), k * atr_frac], axis=1)
        return FoldTarget(spec, blab, {"k": k, "m": m, "not_win_mean": not_win}, cr)
    timeout_mean = float(np.nanmean(tf[tl == 1])) if (tl == 1).any() else 0.0
    cr = np.stack([-m * atr_frac, np.full(len(panel), timeout_mean), k * atr_frac], axis=1)
    return FoldTarget(spec, lab, {"k": k, "m": m, "timeout_mean": timeout_mean}, cr)


def labels_for_frame(frame: pd.DataFrame, spec: TargetSpec, train_mask: np.ndarray,
                     k: Optional[float] = None) -> Tuple[pd.Series, Dict[str, float], int]:
    """Labels of ONE symbol's wizard dataset (index = bar end, columns Open/High/Low/Close), fitted on the rows of
    ``train_mask`` (k and terciles), as used by the dataset-wizard training path. Returns (labels, fitted params,
    look-ahead in bars: the purge length the train/test split must apply).

    ``k`` fixes the triple-barrier take-profit multiple; None chooses it on the training rows (timeout ~ 1/3)."""
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise TypeError("frame must be indexed by the bar end (DatetimeIndex)")
    idx = frame.index
    b = pd.DataFrame({"session": idx.date, "end": idx, "open": frame["Open"].values, "high": frame["High"].values,
                      "low": frame["Low"].values, "close": frame["Close"].values})
    b["bar"] = b.groupby("session").cumcount()
    from app.services.ml_panel import daily_from_bars, daily_context, forward_returns
    _, atr_d = daily_context(b, daily_from_bars(b))
    b["atr_d"] = atr_d.values
    b = pd.concat([b, forward_returns(b, (spec.horizon,))], axis=1)
    b["symbol"] = "X"
    b["row_in_symbol"] = np.arange(len(b))
    train_rows = np.flatnonzero(np.asarray(train_mask, dtype=bool) & (b[f"tgt_idx_{spec.horizon}"].values >= 0))
    if spec.kind == "triple_barrier" and k is not None:
        up, dn = barrier_excursions(b, spec.horizon)
        lab = triple_barrier_labels(up, dn, k, k * spec.ratio)
        ft = FoldTarget(spec, lab, {"k": k, "m": k * spec.ratio}, None)
    else:
        ft = fit_target(b, spec, train_rows)
    lookahead = int((b[f"tgt_idx_{spec.horizon}"] - b["row_in_symbol"]).where(b[f"tgt_idx_{spec.horizon}"] >= 0).max())
    return pd.Series(ft.labels, index=idx, name=f"{spec.kind}_{spec.horizon}"), ft.params, lookahead


def class_shares(labels: np.ndarray, rows: np.ndarray) -> List[float]:
    lab = labels[rows]
    lab = lab[~np.isnan(lab)]
    if not len(lab):
        return [float("nan")] * 3
    return [float((lab == c).mean()) for c in (0, 1, 2)]


def check_balance(spec: TargetSpec, fold_name: str, shares_by_part: Dict[str, Sequence[float]]) -> None:
    """Refuse a classification fold whose smallest class (train or prediction part) is below MIN_CLASS_SHARE."""
    if not spec.is_classification:
        return
    for part, sh in shares_by_part.items():
        if spec.is_binary:
            sh = [sh[0], 1.0, sh[2]]          # class 1 is unused by a two-class target
        if min(sh) < MIN_CLASS_SHARE:
            names = CLASS_NAMES[spec.kind]
            raise TargetBalanceError(
                f"{spec.kind} h={spec.horizon} ratio={spec.ratio:g} fold {fold_name} {part}: smallest class "
                f"{names[int(np.argmin(sh))]} = {min(sh):.1%} < {MIN_CLASS_SHARE:.0%} "
                f"(shares {', '.join(f'{n}={s:.1%}' for n, s in zip(names, sh))})")
