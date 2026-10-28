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

DAILY rows (ML exploration v2, the "daily rows" section at the end): the daily expert decides at session S's close
and trades at S+1's OPEN, so a decision's outcome is open(S+1) -> close(S+H). ``daily_targets`` builds the selection
(session-relative) and timing (per instrument) targets for a whole daily panel indexed [symbol, session];
``fit_daily_target`` fits the fold-dependent ones (terciles, barrier k) on the training rows only and reuses
``fit_target``; ``check_balance`` gains an explicit ``min_count`` mode for class-weighted models.
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
#: ``check_balance(mode="min_count")``: a fold with fewer examples than this in any used class is refused
MIN_CLASS_COUNT = 50
BALANCE_MODES: Tuple[str, ...] = ("share", "min_count")
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


def class_counts(labels: np.ndarray, rows: np.ndarray) -> List[int]:
    """Number of labelled rows of each class (0 / 1 / 2) among ``rows``."""
    lab = labels[rows]
    lab = lab[~np.isnan(lab)]
    return [int((lab == c).sum()) for c in (0, 1, 2)]


def check_balance(spec: TargetSpec, fold_name: str, shares_by_part: Optional[Dict[str, Sequence[float]]] = None, *,
                  mode: str = "share", counts_by_part: Optional[Dict[str, Sequence[int]]] = None,
                  min_count: int = MIN_CLASS_COUNT) -> None:
    """Refuse a classification fold with a class that is too small, in one of two explicit modes:

    * ``mode="share"`` (default; ``shares_by_part``): the smallest class (train or prediction part) is below
      MIN_CLASS_SHARE;
    * ``mode="min_count"`` (``counts_by_part``, from ``class_counts``): some used class has fewer than ``min_count``
      examples. For class-weighted models, where imbalance is handled by the weights and only a class too small to
      learn from is a reason to refuse.

    A two-class target ignores class 1 in both modes."""
    if mode not in BALANCE_MODES:
        raise ValueError(f"check_balance mode must be one of {BALANCE_MODES}, got {mode!r}")
    if mode == "share":
        if shares_by_part is None:
            raise ValueError("check_balance(mode='share') needs shares_by_part")
        if counts_by_part is not None:
            raise ValueError("check_balance(mode='share') does not take counts_by_part; use mode='min_count'")
    else:
        if counts_by_part is None:
            raise ValueError("check_balance(mode='min_count') needs counts_by_part")
        if shares_by_part is not None:
            raise ValueError("check_balance(mode='min_count') does not take shares_by_part; use mode='share'")
        if min_count <= 0:
            raise ValueError("min_count must be > 0")
    if not spec.is_classification:
        return
    names = CLASS_NAMES[spec.kind]
    if mode == "min_count":
        for part, cnt in counts_by_part.items():
            used = [0, 2] if spec.is_binary else [0, 1, 2]
            small = [c for c in used if cnt[c] < min_count]
            if small:
                raise TargetBalanceError(
                    f"{spec.kind} h={spec.horizon} ratio={spec.ratio:g} fold {fold_name} {part}: class "
                    f"{names[small[0]]} has {int(cnt[small[0]])} examples < {min_count} "
                    f"(counts {', '.join(f'{names[c]}={int(cnt[c])}' for c in used)})")
        return
    for part, sh in shares_by_part.items():
        if spec.is_binary:
            sh = [sh[0], 1.0, sh[2]]          # class 1 is unused by a two-class target
        if min(sh) < MIN_CLASS_SHARE:
            raise TargetBalanceError(
                f"{spec.kind} h={spec.horizon} ratio={spec.ratio:g} fold {fold_name} {part}: smallest class "
                f"{names[int(np.argmin(sh))]} = {min(sh):.1%} < {MIN_CLASS_SHARE:.0%} "
                f"(shares {', '.join(f'{n}={s:.1%}' for n, s in zip(names, sh))})")


# ================================================================== daily rows (ML exploration v2)
#: horizons (sessions) of the daily exploration
DAILY_HORIZONS: Tuple[int, ...] = (1, 3, 5, 10, 20)
#: selection track: relevance buckets of a session's relative returns (lambdarank); the quintile target is always 5
RANK_BUCKETS = 5
#: a session with fewer universe members holding a value has no relative return
MIN_SESSION_MEMBERS = 5
DAILY_ATR_WINDOW = 14
DAILY_COLUMNS: Tuple[str, ...] = ("open", "high", "low", "close", "listed", "filled")


@dataclass
class _DailyArrays:
    """The panel as (sessions x symbols) arrays. ``tradable`` = listed, not filled, not ended: a session whose bar is
    a real print."""
    sessions: pd.DatetimeIndex
    symbols: List[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    tradable: np.ndarray
    atr: np.ndarray             # Wilder ATR14 THROUGH session S, price units (known at S's close); NaN when not alive

    @property
    def shape(self) -> Tuple[int, int]:
        return self.close.shape


def _ahead(a: np.ndarray, k: int) -> np.ndarray:
    """``out[t] = a[t + k]`` along the sessions axis; NaN (or False) past the end."""
    out = np.full(a.shape, np.nan if a.dtype.kind == "f" else False, dtype=a.dtype)
    if k < a.shape[0]:
        out[:a.shape[0] - k] = a[k:]
    return out


def _daily_arrays(bars: Dict[str, pd.DataFrame]) -> _DailyArrays:
    from app.services.ml_panel import true_range, wilder
    if not bars:
        raise ValueError("bars is empty")
    symbols = list(bars)
    index = bars[symbols[0]].index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is not None:
        raise TypeError("daily bars must be indexed by naive session timestamps (ml_features.load_daily output)")
    if not index.is_monotonic_increasing or not index.is_unique:
        raise ValueError("daily bars: the session index must be strictly increasing")
    cols: Dict[str, List[np.ndarray]] = {c: [] for c in ("open", "high", "low", "close", "tradable", "atr")}
    for sym in symbols:
        d = bars[sym]
        missing = [c for c in DAILY_COLUMNS if c not in d.columns]
        if missing:
            raise ValueError(f"{sym}: daily bars lack columns {missing}")
        if not d.index.equals(index):
            raise ValueError(f"{sym}: session index differs from {symbols[0]}'s; every symbol needs the same sessions")
        listed = d["listed"].to_numpy(dtype=bool)
        filled = d["filled"].to_numpy(dtype=bool)
        ended = d["ended"].to_numpy(dtype=bool) if "ended" in d.columns else np.zeros(len(d), dtype=bool)
        alive = listed & ~ended
        h, l, c = (d[x].astype(float) for x in ("high", "low", "close"))
        atr = wilder(true_range(h, l, c), DAILY_ATR_WINDOW).to_numpy().copy()
        atr[~alive] = np.nan
        for name, vals in (("open", d["open"].astype(float).to_numpy()), ("high", h.to_numpy()), ("low", l.to_numpy()),
                           ("close", c.to_numpy()), ("tradable", alive & ~filled), ("atr", atr)):
            cols[name].append(vals)
    arr = {k: np.stack(v, axis=1) for k, v in cols.items()}
    return _DailyArrays(index, symbols, arr["open"], arr["high"], arr["low"], arr["close"], arr["tradable"],
                        arr["atr"])


def _check_horizons(horizons: Sequence[int]) -> Tuple[int, ...]:
    hs = tuple(int(h) for h in horizons)
    if not hs or any(h <= 0 for h in hs) or len(set(hs)) != len(hs):
        raise ValueError(f"horizons must be distinct positive session counts, got {tuple(horizons)}")
    return hs


def _entry_exit_returns(a: _DailyArrays, horizon: int) -> Tuple[np.ndarray, np.ndarray]:
    """(entry, ret): entry = open(S+1); ret = close(S+H) / entry - 1, NaN unless S+1 AND S+H are real prints."""
    entry = _ahead(np.where(a.tradable, a.open, np.nan), 1)
    exit_ = _ahead(np.where(a.tradable, a.close, np.nan), horizon)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret = np.where(entry > 0, exit_ / entry - 1.0, np.nan)
    return entry, ret


def _membership(universe: Optional[pd.DataFrame], a: _DailyArrays) -> np.ndarray:
    if universe is None:
        return np.ones(a.shape, dtype=bool)
    gone_s = a.sessions.difference(universe.index)
    gone_c = [s for s in a.symbols if s not in universe.columns]
    if len(gone_s):
        raise ValueError(f"universe has no row for {len(gone_s)} panel sessions, first {gone_s[0].date()}")
    if gone_c:
        raise ValueError(f"universe has no column for panel symbols {gone_c[:5]}")
    m = universe.reindex(index=a.sessions, columns=a.symbols)
    if m.isna().any().any():
        raise ValueError("universe membership holds NaN")
    return m.to_numpy(dtype=bool)


def _buckets(rel: np.ndarray, n_buckets: int) -> np.ndarray:
    """Quantile bucket 0..n_buckets-1 of each value within its row (session); ties share their average rank. NaN
    where ``rel`` is NaN."""
    ranks = pd.DataFrame(rel).rank(axis=1, method="average").to_numpy()
    n = np.sum(~np.isnan(rel), axis=1, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        b = np.floor((ranks - 1.0) / n * n_buckets)
    return np.where(np.isnan(rel), np.nan, np.clip(b, 0, n_buckets - 1))


def _flat(x: np.ndarray) -> np.ndarray:
    """(sessions x symbols) -> symbol-major flat rows, the row order of ``daily_targets``."""
    return np.ascontiguousarray(x.T).reshape(-1)


def daily_targets(bars: Dict[str, pd.DataFrame], horizons: Sequence[int] = DAILY_HORIZONS,
                  universe: Optional[pd.DataFrame] = None, n_buckets: int = RANK_BUCKETS) -> pd.DataFrame:
    """Selection and timing targets of every (symbol, session) decision of a daily panel.

    ``bars``: symbol -> daily OHLCV frame on one shared session index (``ml_features.load_daily`` output: open, high,
    low, close, volume, listed, filled and optionally ended). The expert decides at session S's close and trades at
    S+1's OPEN, so every outcome runs from open(S+1) to the close of S+H. A target is NaN unless both S+1 and S+H
    are real prints (listed, not filled, not ended), and past the data end.

    ``universe``: membership (sessions x symbols, bool); None = every symbol, always. A symbol outside the universe
    on S has no relative target that session (``rel_ret_H`` and everything derived from it are NaN) but keeps its
    timing targets.

    Returns a frame indexed [symbol, session] (symbols in input order, sessions ascending) with, per horizon H:

    * ``ret_H``: close(S+H) / open(S+1) - 1;   ``vol_H``: ATR14% x sqrt(H), the volatility unit. ATR14% is the Wilder
      ATR of the sessions up to and including S over S's close (what the feature store's ``t_atr_pct`` holds), so it
      is known at the decision; ``atr_pct`` is its H-free part;
    * selection: ``rel_ret_H`` = ret_H minus its mean over the session's universe members (NaN when fewer than
      MIN_SESSION_MEMBERS members have a value); ``quintile_H`` 1 = top fifth / 0 = bottom fifth / NaN otherwise;
      ``beats_H`` = rel_ret_H > 0; ``rank_rel_H`` = quantile bucket 0..n_buckets-1 of rel_ret_H within the session
      (lambdarank relevance; a float column holding integers); ``session_group`` = session ordinal (the lambdarank
      group; see ``rank_groups``);
    * timing: ``up_H`` = ret_H > 0; ``z_H`` = ret_H / vol_H clipped at +-REGRESSION_CLIP; the fold-fitted targets
      (terciles, barriers) come from ``fit_daily_target``;
    * ``realvol_H``: std (ddof 1) of the daily log returns of sessions S+1..S+H (NaN for H = 1: one return has no
      spread); ``target_end_H``: the session S+H (NaT past the data end), for the purge."""
    hs = _check_horizons(horizons)
    if n_buckets < 2:
        raise ValueError("n_buckets must be >= 2")
    a = _daily_arrays(bars)
    member = _membership(universe, a)
    T, N = a.shape
    with np.errstate(divide="ignore", invalid="ignore"):
        atr_pct = np.where(a.atr > 0, a.atr / a.close, np.nan)
        log_ret = pd.DataFrame(np.log(a.close)).diff()
    cols: Dict[str, np.ndarray] = {"atr_pct": _flat(atr_pct)}
    cols["session_group"] = _flat(np.repeat(np.arange(T)[:, None], N, axis=1)).astype(np.int64)
    sess_ns = a.sessions.to_numpy("datetime64[ns]")
    for h in hs:
        _, ret = _entry_exit_returns(a, h)
        vol = atr_pct * np.sqrt(h)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = np.where(vol > 0, np.clip(ret / vol, -REGRESSION_CLIP, REGRESSION_CLIP), np.nan)
        vals = np.where(member, ret, np.nan)
        n_val = np.sum(~np.isnan(vals), axis=1)
        mean = np.where(n_val > 0, np.nansum(vals, axis=1) / np.maximum(n_val, 1), np.nan)
        rel = vals - mean[:, None]
        rel[n_val < MIN_SESSION_MEMBERS, :] = np.nan
        top = _buckets(rel, 5)
        rv = log_ret.rolling(h, min_periods=h).std().to_numpy()      # window ending at t = returns of t-h+1..t
        realvol = np.where(np.isnan(ret), np.nan, _ahead(rv, h))
        end = np.full(T, np.datetime64("NaT"), dtype="datetime64[ns]")
        if h < T:
            end[:T - h] = sess_ns[h:]
        cols[f"ret_{h}"] = _flat(ret)
        cols[f"vol_{h}"] = _flat(vol)
        cols[f"rel_ret_{h}"] = _flat(rel)
        cols[f"quintile_{h}"] = _flat(np.where(top == 4, 1.0, np.where(top == 0, 0.0, np.nan)))
        cols[f"beats_{h}"] = _flat(np.where(np.isnan(rel), np.nan, (rel > 0).astype(float)))
        cols[f"rank_rel_{h}"] = _flat(_buckets(rel, n_buckets))
        cols[f"realvol_{h}"] = _flat(realvol)
        cols[f"target_end_{h}"] = np.tile(end, N)
        cols[f"up_{h}"] = _flat(np.where(np.isnan(ret), np.nan, (ret > 0).astype(float)))
        cols[f"z_{h}"] = _flat(z)
    idx = pd.MultiIndex.from_product([a.symbols, a.sessions], names=["symbol", "session"])
    return pd.DataFrame(cols, index=idx)


def rank_groups(df: pd.DataFrame, horizon: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Lambdarank input of ``daily_targets`` rows: (row positions sorted by session, relevance labels (int), group
    sizes). Only rows with a ``rank_rel_H`` are kept; groups are the sessions in ascending order."""
    rel = df[f"rank_rel_{horizon}"].to_numpy()
    rows = np.flatnonzero(~np.isnan(rel))
    grp = df["session_group"].to_numpy()
    order = rows[np.argsort(grp[rows], kind="stable")]
    _, sizes = np.unique(grp[order], return_counts=True)
    return order, rel[order].astype(np.int64), sizes


def daily_excursions(bars: Dict[str, pd.DataFrame], horizon: int) -> Tuple[np.ndarray, np.ndarray]:
    """The daily barrier paths, rows aligned with ``daily_targets(bars, ...)``: ``up[i, j]`` = (max high of sessions
    S+1..S+1+j - open(S+1)) / ATR14(S), ``dn[i, j]`` = (open(S+1) - min low of S+1..S+1+j) / ATR14(S), j = 0..H-1
    (the path starts at the ENTRY open and ends with S+H's bar). NaN rows: S+1 or S+H not a real print, or no ATR."""
    h = _check_horizons([horizon])[0]
    a = _daily_arrays(bars)
    T, N = a.shape
    entry, ret = _entry_exit_returns(a, h)
    ok = ~np.isnan(ret) & (a.atr > 0)
    up = np.full((N, T, h), np.nan, dtype=np.float32)
    dn = np.full((N, T, h), np.nan, dtype=np.float32)
    hi_run = np.full((T, N), -np.inf)
    lo_run = np.full((T, N), np.inf)
    for j in range(h):
        hi_run = np.maximum(hi_run, _ahead(a.high, 1 + j))
        lo_run = np.minimum(lo_run, _ahead(a.low, 1 + j))
        up[:, :, j] = np.where(ok, (hi_run - entry) / a.atr, np.nan).T
        dn[:, :, j] = np.where(ok, (entry - lo_run) / a.atr, np.nan).T
    return up.reshape(N * T, h), dn.reshape(N * T, h)


def multistep_matrix(df: pd.DataFrame, horizons: Sequence[int] = DAILY_HORIZONS) -> np.ndarray:
    """(n, len(horizons)) float array of ``up_H`` (NaN = no outcome), the target of one multi-output model."""
    hs = _check_horizons(horizons)
    missing = [f"up_{h}" for h in hs if f"up_{h}" not in df.columns]
    if missing:
        raise KeyError(f"daily targets lack {missing}")
    return df[[f"up_{h}" for h in hs]].to_numpy(dtype=np.float64)


def daily_fold_rows(df: pd.DataFrame, fold: Fold, horizon: int) -> Tuple[np.ndarray, np.ndarray]:
    """(train, pred) row positions in a ``daily_targets`` frame. A training row's target session ``target_end_H``
    must lie BEFORE the prediction block (the purge gap of one horizon) and the row must have an outcome; prediction
    rows are every session of the block."""
    day = df.index.get_level_values("session").normalize()
    tgt = pd.DatetimeIndex(df[f"target_end_{horizon}"])
    t0, p0, p1 = (pd.Timestamp(d) for d in (fold.train_start, fold.pred_start, fold.pred_end))
    train = (day >= t0) & (day < p0) & ~tgt.isna() & (tgt < p0) & ~np.isnan(df[f"ret_{horizon}"].to_numpy())
    pred = (day >= p0) & (day <= p1)
    return np.flatnonzero(train), np.flatnonzero(pred)


def fit_daily_target(df: pd.DataFrame, spec: TargetSpec, train_mask: np.ndarray,
                     excursions: Optional[Tuple[np.ndarray, np.ndarray]] = None) -> FoldTarget:
    """``fit_target`` for ``daily_targets`` rows: terciles and the barrier k are fitted ONLY on the rows of
    ``train_mask`` (bool, one per row); labels are returned for every row.

    The barrier kinds need ``excursions`` = ``daily_excursions(bars, horizon)`` (rows aligned with ``df``). Labels
    use the ml_targets classes (0 / 1 / 2; the two-class kinds 0 / 2); ``regression`` is ``z_H`` and
    ``forward_return`` is ``ret_H``."""
    h = spec.horizon
    mask = np.asarray(train_mask)
    if mask.dtype != bool or mask.shape != (len(df),):
        raise ValueError(f"train_mask must be a bool array of {len(df)} rows, got {mask.dtype} {mask.shape}")
    for c in (f"ret_{h}", f"z_{h}", "atr_pct"):
        if c not in df.columns:
            raise KeyError(f"daily targets lack {c}; build them with horizon {h}")
    ret = df[f"ret_{h}"].to_numpy(dtype=np.float64)
    if spec.kind == "forward_return":
        return FoldTarget(spec, ret, {}, None)
    if spec.kind == "regression":
        return FoldTarget(spec, df[f"z_{h}"].to_numpy(dtype=np.float64),
                          {"vol_unit": "daily ATR% x sqrt(N)", "clip": REGRESSION_CLIP}, None)
    train_rows = np.flatnonzero(mask & np.isfinite(ret))
    if not len(train_rows):
        raise TargetBalanceError(f"{spec.kind} h={h}: no training row has an outcome")
    if spec.kind == "binary_direction" and not ((ret[train_rows] > 0).any() and (ret[train_rows] <= 0).any()):
        raise TargetBalanceError(f"binary_direction h={h}: the training rows hold a single class")
    panel = pd.DataFrame({f"fwd_ret_{h}": ret, "atr_d": df["atr_pct"].to_numpy(dtype=np.float64), "close": 1.0})
    if spec.kind in ("triple_barrier", "binary_barrier"):
        if excursions is None:
            raise ValueError(f"{spec.kind} needs excursions=daily_excursions(bars, {h})")
        if len(excursions[0]) != len(df) or excursions[0].shape[1] != h:
            raise ValueError(f"excursions {excursions[0].shape} do not match {len(df)} rows x horizon {h}")
    return fit_target(panel, spec, train_rows, excursions)
