"""DeterministicScorer - TECHNICAL section, pure calculators.

All functions are side-effect free and operate on plain pandas Series/DataFrames
sliced to ``<= as_of`` by the caller (no lookahead). Only numpy/pandas.

Evidence base (see workspace/ba2/research-deterministic-scoring.md §1):
vol-adjusted 12-1 momentum (Barroso & Santa-Clara 2015, Daniel & Moskowitz 2016),
SMA200 trend (Faber 2007), Donchian-20 (turtle literature), RSI14 inverted as
the mean-reversion leg (short-term reversal, Jegadeesh 1990), ADX as a trend-
quality gate (Wilder 1978).

SCALARS ARE THE TAIL OF A SERIES
--------------------------------
Every indicator here is a CAUSAL function of rows ``0..t``: a forward recursion
(Wilder smoothing == ``ewm(adjust=False)``), a trailing window, or a pair of
positional reads. Element ``t`` therefore performs the SAME float operation
sequence whether the input stops at ``t`` or continues past it, so computing the
whole series once over a symbol's frame and indexing it by position is
BIT-IDENTICAL to recomputing a scalar from a slice that ends at ``t``.

That is what the ``*_series`` functions below are for, and the scalar functions
are now thin wrappers over them (one implementation, no drift). The expensive
part of this module was never the arithmetic -- it was ~41 pandas Series
constructions per ``technical_score`` call to return six floats, paid once per
symbol per bar (~145k times per GA trial). ``technical_score`` can now be handed
an OHLCV *view* of the full cached frame and will index memoised series instead.

NO LOOKAHEAD. The series are computed over a frame that CONTAINS ROWS AFTER
``as_of``; reading element ``t`` is only safe because every operation here is
backward-looking. Nothing in this module may use a centered window, a ``bfill``,
a reverse scan, a global (whole-series) mean/std, or a ``dropna`` that shifts
alignment. ``tests/test_deterministic_scorer_series_parity.py`` pins that with an
exact ``==`` comparison of every indicator against the truncated-frame scalar,
per date, on real and synthetic data. If you add an indicator here, add it there.
"""
from __future__ import annotations

import math
import threading
import warnings
from collections import OrderedDict
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

# --- Defaults (mirrored by the expert's ExpertSetting defaults) --------------
DEF_MOM_LOOKBACK = 252   # ~12 months of trading days
DEF_MOM_SKIP = 21        # skip the most recent month (short-term reversal)
DEF_VOL_WINDOW = 63      # ~3 months for realized vol scaling
DEF_SMA_TREND = 200
DEF_RSI_PERIOD = 14
DEF_DONCHIAN = 20
DEF_ADX_PERIOD = 14
DEF_ADX_GATE = 25.0

DEF_TW_MOM = 0.45
DEF_TW_D200 = 0.25
DEF_TW_RSI = 0.15
DEF_TW_DON = 0.15

# tanh characteristic scales (tunable via settings)
DEF_SCALE_MOMVOL = 1.5   # vol-adj momentum units (sigma-scaled returns)
DEF_SCALE_D200 = 0.15    # 15% away from SMA200 ~ saturated
DEF_SCALE_RSI = 15.0     # RSI points around 50


def tanh_scale(x: Optional[float], k: float) -> float:
    """Map an unbounded value to (-1, 1) with characteristic scale k>0."""
    if x is None or k <= 0 or not np.isfinite(x):
        return 0.0
    return float(math.tanh(x / k))


def _values(series: pd.Series) -> np.ndarray:
    """The Series' float64 values, without copying when it already is float64."""
    return np.asarray(series.to_numpy(dtype=float, copy=False), dtype=np.float64)


def _tail(arr: np.ndarray) -> Optional[float]:
    """Last element of an indicator series as the scalar contract wants it.

    NaN is the series' encoding of "the scalar returns None" -- every guard the
    scalar functions used to apply (too little history, a non-positive ATR, a
    non-finite ADX) is written into the array as NaN by the ``*_series`` builder,
    so the wrappers below share ONE implementation with the fast path.
    """
    if arr.size == 0:
        return None
    val = arr[-1]
    return float(val) if np.isfinite(val) else None


def _ffill(arr: np.ndarray) -> np.ndarray:
    """Carry the last non-NaN value forward (causal; never reaches backwards)."""
    idx = np.where(np.isnan(arr), 0, np.arange(arr.size))
    np.maximum.accumulate(idx, out=idx)
    return arr[idx]


def momentum_12_1_series(closes: pd.Series, lookback: int = DEF_MOM_LOOKBACK,
                         skip: int = DEF_MOM_SKIP) -> np.ndarray:
    """``momentum_12_1`` at every position, NaN where the scalar returns None."""
    n = 0 if closes is None else len(closes)
    out = np.full(n, np.nan)
    # skip >= lookback is a nonsensical combination the UI can still produce; it
    # used to walk off the front of the series with an IndexError mid-bar.
    if n == 0 or lookback <= 0 or skip < 0 or skip >= lookback or n < lookback:
        return out
    v = _values(closes)
    # At position t the scalar reads v[t - skip] and v[t - lookback + 1]; both
    # are strictly behind t, which is what makes the full-frame form causal.
    recent = v[lookback - 1 - skip:n - skip]
    base = v[:n - lookback + 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        vals = recent / base - 1.0
    vals = np.where((base > 0) & np.isfinite(base) & np.isfinite(recent), vals, np.nan)
    out[lookback - 1:] = vals
    return out


def momentum_12_1(closes: pd.Series, lookback: int = DEF_MOM_LOOKBACK,
                  skip: int = DEF_MOM_SKIP) -> Optional[float]:
    """Classic 12-1 momentum: P[t-skip] / P[t-lookback] - 1.

    Skips the most recent month because short-horizon returns mean-revert
    (microstructure), which degrades momentum (Jegadeesh-Titman).
    """
    if closes is None:
        return None
    return _tail(momentum_12_1_series(closes, lookback, skip))


def realized_vol_daily_series(closes: pd.Series,
                              window: int = DEF_VOL_WINDOW) -> np.ndarray:
    """``realized_vol_daily`` at every position, NaN where the scalar is None.

    Mirrors the scalar exactly, including the two-pass variance pandas uses
    (``avg = values.sum()/count`` then ``sum((avg - values)**2)/(count-1)``), so
    the window sums come out bit-for-bit the same as ``Series.std(ddof=1)``.
    """
    n = 0 if closes is None else len(closes)
    out = np.full(n, np.nan)
    if n == 0:
        return out
    min_hist = max(10, window // 2)
    rets = _values(closes.pct_change())
    # ``dropna`` keeps +/-inf (a zero close), so the mask is isnan, not isfinite:
    # an inf in the window makes the scalar's std NaN and must do so here too.
    valid = ~np.isnan(rets)
    pos = np.flatnonzero(valid)
    r = rets[valid]
    if r.size == 0:
        return out
    contiguous = bool(pos.size and pos[0] == 1 and pos[-1] == n - 1
                      and pos.size == n - 1)
    if contiguous and window > 0 and r.size >= window:
        # Fast path: the only NaN return is the leading one, so the count of
        # returns available at position t is exactly t.
        w = sliding_window_view(r, window)
        cnt = float(window)
        avg = w.sum(axis=1, dtype=np.float64) / cnt
        sqr = (avg[:, None] - w) ** 2
        vals = np.sqrt(sqr.sum(axis=1, dtype=np.float64) / (cnt - 1.0))
        out[window:window + vals.size] = vals
        head = range(1, min(window, n))
    else:
        head = range(1, n)
    for t in head:
        k = int(np.searchsorted(pos, t, side="right"))
        seg = r[max(0, k - window):k] if window > 0 else r[:0]
        if seg.size < 10:
            continue
        cnt = float(seg.size)
        avg = seg.sum(dtype=np.float64) / cnt
        sqr = (avg - seg) ** 2
        out[t] = math.sqrt(sqr.sum(dtype=np.float64) / (cnt - 1.0))
    # Guards: too little price history, or a degenerate/non-finite sigma.
    if min_hist > 1:
        out[:min(min_hist - 1, n)] = np.nan
    out[~(np.isfinite(out) & (out > 0))] = np.nan
    return out


def realized_vol_daily(closes: pd.Series, window: int = DEF_VOL_WINDOW) -> Optional[float]:
    """Daily realized volatility (std of simple returns) over `window` bars."""
    if closes is None:
        return None
    return _tail(realized_vol_daily_series(closes, window))


def vol_adjusted_momentum_series(closes: pd.Series, lookback: int = DEF_MOM_LOOKBACK,
                                 skip: int = DEF_MOM_SKIP,
                                 vol_window: int = DEF_VOL_WINDOW) -> np.ndarray:
    """``vol_adjusted_momentum`` at every position, NaN where the scalar is None."""
    n = 0 if closes is None else len(closes)
    if n == 0:
        return np.full(0, np.nan)
    mom = momentum_12_1_series(closes, lookback, skip)
    sigma = realized_vol_daily_series(closes, vol_window)
    horizon = max(int(lookback) - int(skip), 1)
    sigma_h = sigma * math.sqrt(horizon)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = mom / sigma_h
    return np.where(np.isfinite(sigma_h) & (sigma_h > 0), out, np.nan)


def vol_adjusted_momentum(closes: pd.Series, lookback: int = DEF_MOM_LOOKBACK,
                          skip: int = DEF_MOM_SKIP,
                          vol_window: int = DEF_VOL_WINDOW) -> Optional[float]:
    """Momentum scaled by realized vol - removes most momentum-crash risk and
    makes scores comparable across low/high-vol names.

    THE VOL MUST MATCH THE RETURN'S HORIZON. ``momentum_12_1`` is a TOTAL RETURN over
    ``lookback - skip`` trading days (~231 by default), while ``realized_vol_daily`` is a ONE-DAY
    sigma. Dividing one by the other mixes units and inflates the result by sqrt(horizon) ~ 15x.

    Measured 2026-08-11 over 160 symbol-dates (20 large/mid caps, 8 quarter-ends 2021-2024) with
    the unscaled denominator: median 8.62, p95 53.6, max 108.9. Those go through
    ``tanh(x / scale_momvol)`` with scale 1.5 -- the value the module docstring calls "vol-adj
    momentum units" -- so 83% of observations came out at |1.0|. The single heaviest component
    (weight 0.45) was therefore not a score at all but the SIGN of momentum, carrying no ranking
    information, while the other three legs varied normally (sd 0.55-0.66).

    Scaling sigma to the same horizon restores the range the constant was written for: median
    0.57, p95 3.53, and saturation falls to 4%. This is a units fix, not a re-tuning -- the 1.5
    scale was always correct for the intended quantity.
    """
    if closes is None:
        return None
    return _tail(vol_adjusted_momentum_series(closes, lookback, skip, vol_window))


def distance_to_sma_series(closes: pd.Series,
                           period: int = DEF_SMA_TREND) -> np.ndarray:
    """``distance_to_sma`` at every position, NaN where the scalar is None."""
    n = 0 if closes is None else len(closes)
    out = np.full(n, np.nan)
    if n == 0 or period <= 0 or n < period:
        return out
    v = _values(closes)
    w = sliding_window_view(v, period)
    # ``Series.mean()`` is ``values.sum()/count``; the same sum over the same
    # contiguous window reproduces it bit-for-bit (pinned by the parity test).
    # With a NaN in the window pandas zeroes it and divides by the surviving
    # count (nanops.nanmean), which is what the second branch reproduces.
    if np.isnan(v).any():
        holes = np.isnan(w)
        count = period - holes.sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            sma = np.where(holes, 0.0, w).sum(axis=1) / count
    else:
        sma = w.sum(axis=1) / period
    last = v[period - 1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        vals = last / sma - 1.0
    out[period - 1:] = np.where(np.isfinite(sma) & (sma > 0), vals, np.nan)
    return out


def distance_to_sma(closes: pd.Series, period: int = DEF_SMA_TREND) -> Optional[float]:
    """P / SMA(period) - 1. Positive = above the trend SMA."""
    if closes is None:
        return None
    return _tail(distance_to_sma_series(closes, period))


def rsi_wilder_series(closes: pd.Series, period: int = DEF_RSI_PERIOD) -> np.ndarray:
    """``rsi_wilder`` at every position, NaN where the scalar is None.

    Wilder smoothing is ``ewm(alpha=1/period, adjust=False)`` -- a pure forward
    recursion, so element t is unchanged by rows after t. The ``dropna`` the
    scalar applies compacts the deltas; positions whose delta was dropped read
    the last computed value (exactly what ``.iloc[-1]`` of the compacted ewm
    returns for such a slice), which is a forward carry, never a backfill.
    """
    n = 0 if closes is None else len(closes)
    out = np.full(n, np.nan)
    if n < period + 1 or period <= 0:
        return out
    deltas = _values(closes.diff())
    valid = ~np.isnan(deltas)
    compact = pd.Series(deltas[valid])
    if compact.empty:
        return out
    gain = compact.clip(lower=0.0)
    loss = (-compact).clip(lower=0.0)
    avg_gain = _values(gain.ewm(alpha=1.0 / period, adjust=False).mean())
    avg_loss = _values(loss.ewm(alpha=1.0 / period, adjust=False).mean())
    with np.errstate(divide="ignore", invalid="ignore"):
        vals = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    vals = np.where(avg_loss == 0.0, 100.0, vals)
    placed = np.full(n, np.nan)
    placed[np.flatnonzero(valid)] = vals
    out[period:] = _ffill(placed)[period:]
    return out


def rsi_wilder(closes: pd.Series, period: int = DEF_RSI_PERIOD) -> Optional[float]:
    """RSI with Wilder smoothing (the standard)."""
    if closes is None:
        return None
    return _tail(rsi_wilder_series(closes, period))


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def _true_range(highs: pd.Series, lows: pd.Series, closes: pd.Series) -> pd.Series:
    return pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)


def _common_tail(highs: pd.Series, lows: pd.Series, closes: pd.Series) -> tuple:
    n = min(len(highs), len(lows), len(closes))
    return n, highs.iloc[-n:], lows.iloc[-n:], closes.iloc[-n:]


def adx_wilder_series(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                      period: int = DEF_ADX_PERIOD) -> np.ndarray:
    """``adx_wilder`` at every position. Every step is elementwise, a backward
    shift, or a forward ewm, so no row after t touches element t."""
    n, highs, lows, closes = _common_tail(highs, lows, closes)
    out = np.full(n, np.nan)
    if n == 0 or period <= 0:
        return out
    up_move = highs.diff()
    down_move = -lows.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    atr = _wilder_smooth(_true_range(highs, lows, closes), period)
    plus_di = 100.0 * _wilder_smooth(plus_dm, period) / atr.replace(0.0, np.nan)
    minus_di = 100.0 * _wilder_smooth(minus_dm, period) / atr.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    vals = _values(_wilder_smooth(dx, period))
    # The scalar refuses to answer with less than 2*period bars.
    warm = min(2 * period - 1, n)
    out[warm:] = vals[warm:]
    out[~np.isfinite(out)] = np.nan
    return out


def adx_wilder(highs: pd.Series, lows: pd.Series, closes: pd.Series,
               period: int = DEF_ADX_PERIOD) -> Optional[float]:
    """Average Directional Index (Wilder). Returns the final ADX value."""
    return _tail(adx_wilder_series(highs, lows, closes, period))


def atr_wilder_series(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                      period: int = DEF_ADX_PERIOD) -> np.ndarray:
    """``atr_wilder`` at every position (elementwise + forward ewm only)."""
    n, highs, lows, closes = _common_tail(highs, lows, closes)
    out = np.full(n, np.nan)
    if n == 0 or period <= 0:
        return out
    vals = _values(_wilder_smooth(_true_range(highs, lows, closes), period))
    warm = min(period, n)
    out[warm:] = vals[warm:]
    out[~(np.isfinite(out) & (out > 0))] = np.nan
    return out


def atr_wilder(highs: pd.Series, lows: pd.Series, closes: pd.Series,
               period: int = DEF_ADX_PERIOD) -> Optional[float]:
    """Average True Range (Wilder)."""
    return _tail(atr_wilder_series(highs, lows, closes, period))


def donchian_state_series(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                          period: int = DEF_DONCHIAN) -> np.ndarray:
    """``donchian_state`` at every position.

    The channel at position t spans rows t-period .. t-1 -- the scalar's
    ``iloc[-(period+1):-1]`` window, never today's bar and never a future one.
    """
    n, highs, lows, closes = _common_tail(highs, lows, closes)
    out = np.full(n, np.nan)
    if n == 0 or period <= 0 or n < period + 1:
        return out
    hv, lv, close = _values(highs), _values(lows), _values(closes)
    # Row j of the sliding view spans j .. j+period-1, i.e. the channel for the
    # bar at j+period. ``Series.max()`` skips NaN (skipna=True), so NaN-bearing
    # highs/lows take the nan-aware reduction to stay exact.
    wh = sliding_window_view(hv, period)[:n - period]
    wl = sliding_window_view(lv, period)[:n - period]
    if np.isnan(hv).any() or np.isnan(lv).any():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN window -> NaN
            upper, lower = np.nanmax(wh, axis=1), np.nanmin(wl, axis=1)
    else:
        upper, lower = wh.max(axis=1), wl.min(axis=1)
    cl = close[period:]
    with np.errstate(divide="ignore", invalid="ignore"):
        inside = 2.0 * (cl - lower) / (upper - lower) - 1.0
    out[period:] = np.where(cl > upper, 1.0,
                            np.where(cl < lower, -1.0,
                                     np.where(upper <= lower, 0.0, inside)))
    out[~np.isfinite(out)] = np.nan
    return out


def donchian_state(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                   period: int = DEF_DONCHIAN) -> Optional[float]:
    """Breakout state vs the trailing channel (excluding today's bar):
    +1 close above the channel high, -1 below the channel low, else a
    position-in-channel value in (-1, 1)."""
    return _tail(donchian_state_series(highs, lows, closes, period))


# ---------------------------------------------------------------------------
# Per-symbol indicator SERIES memo
# ---------------------------------------------------------------------------
# The whole point of the series forms: build each indicator ONCE per
# (symbol, frame, parameters) and index it by bar position, instead of rebuilding
# ~41 pandas Series per bar to return six floats.
#
# STALENESS. The key carries the frame's own fingerprint, supplied by the caller
# (``data.OhlcvView``: a monotonic epoch stamped when the cached payload was
# stored, plus its row count and last bar date). A frame that changed cannot hit
# an entry built from the old one, so the live path -- which re-runs as new bars
# arrive -- can never be served yesterday's indicators. ``data.reset_caches()``
# (tests, and the live /api/reload path) drops this store with the rest.
#
# SIZE. One float64 array per indicator per symbol: ~6 x 97 x 3800 x 8B ~ 18 MB
# for a full GA universe. The LRU cap bounds a long-lived worker that walks many
# parameter sets across trials.
_SERIES_MEMO: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
_SERIES_MEMO_MAX = 1200
_SERIES_MEMO_LOCK = threading.RLock()
_SERIES_MEMO_STATS: Dict[str, int] = {"hit": 0, "miss": 0, "evicted": 0}


def reset_series_memo() -> None:
    """Drop every memoised indicator series (called by ``data.reset_caches``)."""
    with _SERIES_MEMO_LOCK:
        _SERIES_MEMO.clear()
        for k in _SERIES_MEMO_STATS:
            _SERIES_MEMO_STATS[k] = 0


def series_memo_stats() -> Dict[str, int]:
    """A copy of the memo counters (tests + the profiling harness)."""
    with _SERIES_MEMO_LOCK:
        return dict(_SERIES_MEMO_STATS)


def _memoised(key: tuple, build) -> np.ndarray:
    with _SERIES_MEMO_LOCK:
        hit = _SERIES_MEMO.get(key)
        if hit is not None:
            _SERIES_MEMO.move_to_end(key)
            _SERIES_MEMO_STATS["hit"] += 1
            return hit
    # Built outside the lock: a duplicate build under contention is wasted work,
    # never a wrong answer (these are pure functions of the frame).
    arr = build()
    arr.flags.writeable = False
    with _SERIES_MEMO_LOCK:
        _SERIES_MEMO[key] = arr
        _SERIES_MEMO.move_to_end(key)
        _SERIES_MEMO_STATS["miss"] += 1
        while len(_SERIES_MEMO) > _SERIES_MEMO_MAX:
            _SERIES_MEMO.popitem(last=False)
            _SERIES_MEMO_STATS["evicted"] += 1
    return arr


class _ViewSource:
    """Indicator values read out of memoised full-frame series at one position.

    ``view`` is a ``data.OhlcvView`` (duck-typed: ``.symbol``, ``.fingerprint``,
    ``.close_s`` / ``.high_s`` / ``.low_s``). ``pos`` is the bar being decided;
    only rows <= pos can influence it, which is the property the parity test pins.
    """

    __slots__ = ("view", "pos")

    def __init__(self, view: Any, pos: int) -> None:
        self.view = view
        self.pos = pos

    def _at(self, name: str, params: tuple, build) -> Optional[float]:
        key = (self.view.symbol, self.view.fingerprint, name, params)
        arr = _memoised(key, build)
        if self.pos >= arr.size:
            # Unreachable: position_of_prefix already bounded pos by the frame's
            # row count. Raising rather than returning None because a silent
            # None here would drop an indicator out of the weighted score and
            # quietly change a trading decision.
            raise RuntimeError(
                f"DeterministicScorer {name}: bar {self.pos} is outside the "
                f"{arr.size}-row series for {self.view.symbol} "
                f"(fingerprint {self.view.fingerprint})")
        val = arr[self.pos]
        return float(val) if np.isfinite(val) else None

    def momvol(self, lookback: int, skip: int, vol_window: int) -> Optional[float]:
        return self._at("momvol", (lookback, skip, vol_window),
                        lambda: vol_adjusted_momentum_series(
                            self.view.close_s, lookback, skip, vol_window))

    def d200(self, period: int) -> Optional[float]:
        return self._at("d200", (period,),
                        lambda: distance_to_sma_series(self.view.close_s, period))

    def rsi(self, period: int) -> Optional[float]:
        return self._at("rsi", (period,),
                        lambda: rsi_wilder_series(self.view.close_s, period))

    def donchian(self, period: int) -> Optional[float]:
        return self._at("don", (period,),
                        lambda: donchian_state_series(
                            self.view.high_s, self.view.low_s, self.view.close_s, period))

    def adx(self, period: int) -> Optional[float]:
        return self._at("adx", (period,),
                        lambda: adx_wilder_series(
                            self.view.high_s, self.view.low_s, self.view.close_s, period))

    def atr(self, period: int) -> Optional[float]:
        return self._at("atr", (period,),
                        lambda: atr_wilder_series(
                            self.view.high_s, self.view.low_s, self.view.close_s, period))


class _SliceSource:
    """The original per-slice path: recompute each indicator from the slice.

    Still the only path for callers that hand ``technical_score`` a bare frame
    (tests, tools, a replayed bundle whose cached frame is gone). It produces the
    same numbers as ``_ViewSource`` -- that equality is the whole design and is
    pinned bit-for-bit by ``test_deterministic_scorer_series_parity.py``.
    """

    __slots__ = ("closes", "highs", "lows")

    def __init__(self, df: pd.DataFrame) -> None:
        self.closes = df["Close"]
        self.highs = df["High"]
        self.lows = df["Low"]

    def momvol(self, lookback: int, skip: int, vol_window: int) -> Optional[float]:
        return vol_adjusted_momentum(self.closes, lookback, skip, vol_window)

    def d200(self, period: int) -> Optional[float]:
        return distance_to_sma(self.closes, period)

    def rsi(self, period: int) -> Optional[float]:
        return rsi_wilder(self.closes, period)

    def donchian(self, period: int) -> Optional[float]:
        return donchian_state(self.highs, self.lows, self.closes, period)

    def adx(self, period: int) -> Optional[float]:
        return adx_wilder(self.highs, self.lows, self.closes, period)

    def atr(self, period: int) -> Optional[float]:
        return atr_wilder(self.highs, self.lows, self.closes, period)


_VIEW_REJECTED_WARNED = False


def _warn_view_rejected(symbol: Optional[str], reason: str) -> None:
    """Say it once when a supplied view could NOT be used.

    The fallback is not a wrong answer -- both sources compute the same floats --
    but it IS the fast path silently not engaging, which in a GA grid looks like
    nothing at all except a run that never finishes. One line per process.
    """
    global _VIEW_REJECTED_WARNED
    if _VIEW_REJECTED_WARNED:
        return
    _VIEW_REJECTED_WARNED = True
    from ba2_common.logger import logger
    logger.warning(
        "DeterministicScorer technical_score: OHLCV view rejected for %s (%s) -- "
        "falling back to the per-slice indicators. Results are unchanged; the "
        "per-symbol series memo is simply not being used.", symbol, reason)


def _reset_view_warning() -> None:
    """Re-arm the once-per-process warning (tests)."""
    global _VIEW_REJECTED_WARNED
    _VIEW_REJECTED_WARNED = False


def _indicator_source(df: pd.DataFrame, view: Any):
    if view is None:
        return _SliceSource(df)
    pos = view.position_of_prefix(df)
    if pos is None:
        _warn_view_rejected(getattr(view, "symbol", None), "slice is not a prefix of the frame")
        return _SliceSource(df)
    return _ViewSource(view, pos)


def technical_score(df: pd.DataFrame, s: Dict[str, Any],
                    view: Any = None) -> Dict[str, Any]:
    """Compute the TECHNICAL section score in [-1, 1].

    `df` needs columns Close/High/Low, already sliced to <= as_of, ascending by
    date. `s` = resolved settings dict. Returns the score + all components for
    the audit trail.

    ``view`` is an optional ``data.OhlcvView`` over the FULL cached frame `df`
    was sliced out of. When given (and when `df` really is a prefix of it), the
    indicators are read out of per-symbol memoised series instead of being
    rebuilt from the slice -- the same floats, ~100x less pandas. Without it, or
    if the prefix check fails, the original per-slice path runs unchanged.
    """
    src = _indicator_source(df, view)

    scale_momvol = float(s.get("scale_momvol", DEF_SCALE_MOMVOL))
    scale_d200 = float(s.get("scale_d200", DEF_SCALE_D200))
    scale_rsi = float(s.get("scale_rsi", DEF_SCALE_RSI))
    adx_gate = float(s.get("adx_gate", DEF_ADX_GATE))

    momvol = src.momvol(
        int(s.get("mom_lookback_days", DEF_MOM_LOOKBACK)),
        int(s.get("mom_skip_days", DEF_MOM_SKIP)), int(s.get("vol_window", DEF_VOL_WINDOW)))
    d200 = src.d200(int(s.get("sma_trend_period", DEF_SMA_TREND)))
    rsi = src.rsi(int(s.get("rsi_period", DEF_RSI_PERIOD)))
    don = src.donchian(int(s.get("donchian_period", DEF_DONCHIAN)))
    adx = src.adx(int(s.get("adx_period", DEF_ADX_PERIOD)))
    atr = src.atr(int(s.get("atr_period", DEF_ADX_PERIOD)))

    # ADX gate: when the market is NOT trending, lean away from trend signals
    # toward the mean-reversion leg (and vice versa). Weights are re-normalized,
    # so this only SHIFTS emphasis, never invents weight.
    trending = adx is not None and adx >= adx_gate
    tw_mom = float(s.get("tw_mom", DEF_TW_MOM))
    tw_d200 = float(s.get("tw_d200", DEF_TW_D200))
    tw_rsi = float(s.get("tw_rsi", DEF_TW_RSI))
    tw_don = float(s.get("tw_don", DEF_TW_DON))
    rsi_boost = float(s.get("adx_rsi_boost", 2.0))
    if not trending:
        tw_rsi = tw_rsi * rsi_boost

    components: Dict[str, tuple] = {
        # name -> (weight, raw value, normalized value)
        "momentum_vol_adj": (tw_mom, momvol, tanh_scale(momvol, scale_momvol)),
        "dist_sma_trend": (tw_d200, d200, tanh_scale(d200, scale_d200)),
        "rsi_meanrev": (tw_rsi, rsi, tanh_scale(50.0 - rsi, scale_rsi) if rsi is not None else 0.0),
        "donchian_breakout": (tw_don, don, don if don is not None else 0.0),
    }
    total_w = sum(w for w, raw, _ in components.values() if raw is not None)
    if total_w <= 0:
        return {"score": 0.0, "components": {}, "adx": adx, "atr": atr,
                "trending": trending, "n_signals": 0}
    score = sum(w * norm for w, raw, norm in components.values() if raw is not None) / total_w
    return {
        "score": float(np.clip(score, -1.0, 1.0)),
        "components": {k: {"weight": w, "raw": raw, "normalized": norm}
                       for k, (w, raw, norm) in components.items()},
        "adx": adx, "atr": atr, "trending": trending,
        "n_signals": sum(1 for _, raw, _ in components.values() if raw is not None),
    }
