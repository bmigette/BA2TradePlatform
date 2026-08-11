"""DeterministicScorer - TECHNICAL section, pure calculators.

All functions are side-effect free and operate on plain pandas Series/DataFrames
sliced to ``<= as_of`` by the caller (no lookahead). Only numpy/pandas.

Evidence base (see workspace/ba2/research-deterministic-scoring.md §1):
vol-adjusted 12-1 momentum (Barroso & Santa-Clara 2015, Daniel & Moskowitz 2016),
SMA200 trend (Faber 2007), Donchian-20 (turtle literature), RSI14 inverted as
the mean-reversion leg (short-term reversal, Jegadeesh 1990), ADX as a trend-
quality gate (Wilder 1978).
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

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


def momentum_12_1(closes: pd.Series, lookback: int = DEF_MOM_LOOKBACK,
                  skip: int = DEF_MOM_SKIP) -> Optional[float]:
    """Classic 12-1 momentum: P[t-skip] / P[t-lookback] - 1.

    Skips the most recent month because short-horizon returns mean-revert
    (microstructure), which degrades momentum (Jegadeesh-Titman).
    """
    # skip >= lookback is a nonsensical combination the UI can still produce; it
    # used to walk off the front of the series with an IndexError mid-bar.
    if closes is None or lookback <= 0 or skip < 0 or skip >= lookback:
        return None
    if len(closes) < lookback:
        return None
    p_recent = float(closes.iloc[-1 - skip]) if skip > 0 else float(closes.iloc[-1])
    p_base = float(closes.iloc[-lookback])
    if p_base <= 0 or not np.isfinite(p_base) or not np.isfinite(p_recent):
        return None
    return p_recent / p_base - 1.0


def realized_vol_daily(closes: pd.Series, window: int = DEF_VOL_WINDOW) -> Optional[float]:
    """Daily realized volatility (std of simple returns) over `window` bars."""
    if closes is None or len(closes) < max(10, window // 2):
        return None
    rets = closes.pct_change().dropna().iloc[-window:]
    if len(rets) < 10:
        return None
    sigma = float(rets.std(ddof=1))
    return sigma if np.isfinite(sigma) and sigma > 0 else None


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
    mom = momentum_12_1(closes, lookback, skip)
    sigma = realized_vol_daily(closes, vol_window)
    if mom is None or sigma is None:
        return None
    horizon = max(int(lookback) - int(skip), 1)
    sigma_h = sigma * math.sqrt(horizon)
    if sigma_h <= 0 or not np.isfinite(sigma_h):
        return None
    return mom / sigma_h


def distance_to_sma(closes: pd.Series, period: int = DEF_SMA_TREND) -> Optional[float]:
    """P / SMA(period) - 1. Positive = above the trend SMA."""
    if closes is None or len(closes) < period:
        return None
    sma = float(closes.iloc[-period:].mean())
    last = float(closes.iloc[-1])
    if sma <= 0 or not np.isfinite(sma):
        return None
    return last / sma - 1.0


def rsi_wilder(closes: pd.Series, period: int = DEF_RSI_PERIOD) -> Optional[float]:
    """RSI with Wilder smoothing (the standard)."""
    if closes is None or len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder's smoothing == EMA with alpha = 1/period
    avg_gain = float(gain.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])
    avg_loss = float(loss.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def adx_wilder(highs: pd.Series, lows: pd.Series, closes: pd.Series,
               period: int = DEF_ADX_PERIOD) -> Optional[float]:
    """Average Directional Index (Wilder). Returns the final ADX value."""
    n = min(len(highs), len(lows), len(closes))
    if n < 2 * period:
        return None
    highs, lows, closes = highs.iloc[-n:], lows.iloc[-n:], closes.iloc[-n:]
    up_move = highs.diff()
    down_move = -lows.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = _wilder_smooth(tr, period)
    plus_di = 100.0 * _wilder_smooth(plus_dm, period) / atr.replace(0.0, np.nan)
    minus_di = 100.0 * _wilder_smooth(minus_dm, period) / atr.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx = _wilder_smooth(dx, period)
    val = float(adx.iloc[-1])
    return val if np.isfinite(val) else None


def atr_wilder(highs: pd.Series, lows: pd.Series, closes: pd.Series,
               period: int = DEF_ADX_PERIOD) -> Optional[float]:
    """Average True Range (Wilder)."""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    highs, lows, closes = highs.iloc[-n:], lows.iloc[-n:], closes.iloc[-n:]
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = _wilder_smooth(tr, period)
    val = float(atr.iloc[-1])
    return val if np.isfinite(val) and val > 0 else None


def donchian_state(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                   period: int = DEF_DONCHIAN) -> Optional[float]:
    """Breakout state vs the trailing channel (excluding today's bar):
    +1 close above the channel high, -1 below the channel low, else a
    position-in-channel value in (-1, 1)."""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    h_window = highs.iloc[-(period + 1):-1]
    l_window = lows.iloc[-(period + 1):-1]
    close = float(closes.iloc[-1])
    upper = float(h_window.max())
    lower = float(l_window.min())
    if close > upper:
        return 1.0
    if close < lower:
        return -1.0
    if upper <= lower:
        return 0.0
    return 2.0 * (close - lower) / (upper - lower) - 1.0


def technical_score(df: pd.DataFrame, s: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the TECHNICAL section score in [-1, 1].

    `df` needs columns Close/High/Low, already sliced to <= as_of, ascending by
    date. `s` = resolved settings dict. Returns the score + all components for
    the audit trail.
    """
    closes = df["Close"]
    highs, lows = df["High"], df["Low"]

    scale_momvol = float(s.get("scale_momvol", DEF_SCALE_MOMVOL))
    scale_d200 = float(s.get("scale_d200", DEF_SCALE_D200))
    scale_rsi = float(s.get("scale_rsi", DEF_SCALE_RSI))
    adx_gate = float(s.get("adx_gate", DEF_ADX_GATE))

    momvol = vol_adjusted_momentum(
        closes, int(s.get("mom_lookback_days", DEF_MOM_LOOKBACK)),
        int(s.get("mom_skip_days", DEF_MOM_SKIP)), int(s.get("vol_window", DEF_VOL_WINDOW)))
    d200 = distance_to_sma(closes, int(s.get("sma_trend_period", DEF_SMA_TREND)))
    rsi = rsi_wilder(closes, int(s.get("rsi_period", DEF_RSI_PERIOD)))
    don = donchian_state(highs, lows, closes, int(s.get("donchian_period", DEF_DONCHIAN)))
    adx = adx_wilder(highs, lows, closes, int(s.get("adx_period", DEF_ADX_PERIOD)))
    atr = atr_wilder(highs, lows, closes, int(s.get("atr_period", DEF_ADX_PERIOD)))

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
