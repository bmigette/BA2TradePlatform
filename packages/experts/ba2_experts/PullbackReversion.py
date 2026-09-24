"""Short-horizon pullback reversion: buy an oversold dip in an uptrend (long), or sell an
overbought rally in a downtrend (short). Research-only.

``pullback_signal`` is the pure, causal decision for the LAST bar of a completed daily history.
Its semantics mirror the feasibility probe ``test_files/pullback_feasibility_20260924.py``
(``rsi``, ``entry_ok``, ``exit_signal``); the market-condition gates read the platform's own
calculators over the 128-bar window ending at that bar, exactly as the live gate would.
Missing, short or corrupt inputs raise ``ValueError``: the function never answers "none" for a
bar it could not evaluate.

The core runs on numpy arrays and Python floats: it is called per symbol per bar in the GA.
"""
from __future__ import annotations

import math
from numbers import Real
from typing import Mapping, Optional

import numpy as np
import pandas as pd

from ba2_common.core.market_conditions import (
    STATUS_INVALID_PRICES, STATUS_VALID, STRUCTURE_STATE_CODES, STRUCTURE_STATE_NONE_CODE, WINDOW,
    compute_chart_structure, compute_market_conditions)

DIRECTIONS = ("long", "short")
TREND_GATES = ("sma200", "slope_ohlcv_v1", "sma200_and_spy")
EXIT_MODES = ("sma5", "rsi", "sma5_or_choch", "time")
SMA_TREND = 200
SMA_EXIT = 5
#: Bars the function needs: SMA200 always; the 128-bar calculator window fits inside it.
MIN_BARS = max(SMA_TREND, WINDOW)
_OHLCV = ("Open", "High", "Low", "Close", "Volume")
_STATE_NAMES = {float(code): name for name, code in STRUCTURE_STATE_CODES.items()}
_STATE_NAMES[STRUCTURE_STATE_NONE_CODE] = "none"


def _choice(settings: Mapping, key: str, options) -> str:
    if key not in settings:
        raise ValueError(f"Missing setting {key!r}")
    value = settings[key]
    if value not in options:
        raise ValueError(f"Setting {key!r} must be one of {options}, got {value!r}")
    return value


def _number(settings: Mapping, key: str, lo: float, hi: float, integer: bool = False):
    if key not in settings:
        raise ValueError(f"Missing setting {key!r}")
    value = settings[key]
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ValueError(f"Setting {key!r} must be a finite number, got {value!r}")
    if integer:
        if float(value) != int(value):
            raise ValueError(f"Setting {key!r} must be an integer, got {value!r}")
        value = int(value)
    else:
        value = float(value)
    if not lo <= value <= hi:
        raise ValueError(f"Setting {key!r} must be in [{lo}, {hi}], got {value!r}")
    return value


def _closes(bars, name: str) -> np.ndarray:
    """The validated close array of a completed daily history (DatetimeIndex, strictly
    ascending sessions, every close finite and positive)."""
    if not isinstance(bars, pd.DataFrame):
        raise ValueError(f"{name} must be a DataFrame, got {type(bars).__name__}")
    missing = [k for k in _OHLCV if k not in bars.columns]
    if missing:
        raise ValueError(f"{name} is missing columns {missing}")
    if not isinstance(bars.index, pd.DatetimeIndex):
        # A provider frame carries a Date column on a RangeIndex: ordering and the SPY session
        # check would then compare row positions and pass silently.
        raise ValueError(f"{name} must be indexed by session date (DatetimeIndex), "
                         f"got {type(bars.index).__name__}")
    if len(bars) < MIN_BARS:
        raise ValueError(f"Insufficient history in {name}: {len(bars)} bars, need {MIN_BARS}")
    if not (bars.index.is_monotonic_increasing and bars.index.is_unique):
        raise ValueError(f"{name} index must be strictly ascending by session")
    try:
        close = np.asarray(bars["Close"], dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Non-numeric close in {name}: {exc}") from exc
    bad = ~(np.isfinite(close) & (close > 0))
    if bad.any():
        i = int(np.flatnonzero(bad)[-1])
        raise ValueError(f"Invalid close in {name} at {bars.index[i]}: {close[i]!r}")
    return close


def _wilder_rsi(close: np.ndarray, period: int) -> float:
    """Wilder RSI of the last bar: the recursion of the probe's ``rsi()``, i.e. pandas
    ``ewm(alpha=1/n, adjust=False)`` seeded on the first close-to-close change.

    With no down move in the smoothed history the RSI is 100. The probe yields NaN there and
    never acts on that bar; here a long in ``rsi`` exit mode exits. The short-entry RSI
    condition also holds, but such a history closes above its SMA200 with a positive slope, so
    no short trend gate passes. With no move at all the RSI is undefined and raises."""
    alpha = 1.0 / period
    keep = 1.0 - alpha
    values = close.tolist()
    change = values[1] - values[0]
    up = change if change > 0 else 0.0
    down = -change if change < 0 else 0.0
    prev = values[1]
    for price in values[2:]:
        change = price - prev
        prev = price
        up = keep * up + alpha * (change if change > 0 else 0.0)
        down = keep * down + alpha * (-change if change < 0 else 0.0)
    if down == 0:
        if up == 0:
            raise ValueError("RSI undefined: no price change in the history")
        return 100.0
    return 100.0 - 100.0 / (1.0 + up / down)


def _window(bars: pd.DataFrame, close: np.ndarray):
    """The last ``WINDOW`` bars as the five float arrays the calculators take."""
    tail = bars.iloc[-WINDOW:]
    return (tail["Open"].to_numpy(dtype=float), tail["High"].to_numpy(dtype=float),
            tail["Low"].to_numpy(dtype=float), close[-WINDOW:],
            tail["Volume"].to_numpy(dtype=float))


def _measured(obs, what: str):
    """``obs.value`` when valid; ``None`` when the calculator could not measure it; a
    ``ValueError`` when it found corrupt bars (that is bad data, not an absent signal)."""
    if obs.status == STATUS_VALID:
        return obs.value
    if obs.status == STATUS_INVALID_PRICES:
        raise ValueError(f"Corrupt bars in the {WINDOW}-bar window for {what}: {obs.reason}")
    return None


def _same_session(bars: pd.DataFrame, spy_bars: pd.DataFrame) -> None:
    tz, spy_tz = bars.index.tz, spy_bars.index.tz
    if str(tz) != str(spy_tz):
        raise ValueError(f"Timezone mismatch: bars index is {tz or 'tz-naive'}, "
                         f"spy_bars index is {spy_tz or 'tz-naive'}")
    if spy_bars.index[-1] != bars.index[-1]:
        raise ValueError(f"spy_bars end on {spy_bars.index[-1]}, bars on {bars.index[-1]}: "
                         "the market regime must be read on the decision session")


def pullback_signal(bars: pd.DataFrame, settings: Mapping,
                    spy_bars: Optional[pd.DataFrame] = None) -> dict:
    """Entry / exit / none for the last bar of ``bars`` (completed daily OHLCV on a
    DatetimeIndex, ascending).

    ``spy_bars`` is required by ``trend_gate == "sma200_and_spy"`` only; it must end on the same
    session as ``bars``. Entry beats exit on the same bar; ``exit_mode == "time"`` never signals
    (the max-hold rule owns that exit). In ``sma5_or_choch`` mode ``structure_state`` is always
    reported ('bull'/'bear'/'none', or None when unmeasurable); otherwise it is None."""
    direction = _choice(settings, "direction", DIRECTIONS)
    gate = _choice(settings, "trend_gate", TREND_GATES)
    period = _number(settings, "rsi_period", 2, 5, integer=True)
    threshold = _number(settings, "entry_threshold", 1.0, 30.0)
    exit_mode = _choice(settings, "exit_mode", EXIT_MODES)
    rsi_exit = _number(settings, "rsi_exit", 50.0, 90.0)
    long = direction == "long"

    close = _closes(bars, "bars")
    last = float(close[-1])
    sma200 = float(close[-SMA_TREND:].mean())
    sma5 = float(close[-SMA_EXIT:].mean())
    rsi = _wilder_rsi(close, period)
    window = None
    notes = []

    if gate == "slope_ohlcv_v1":
        window = _window(bars, close)
        slope = _measured(compute_market_conditions(*window).trend_slope, "the trend slope")
        if slope is None:
            trend_ok = False
            notes.append("trend slope unmeasurable")
        else:
            trend_ok = slope > 0 if long else slope < 0
            notes.append(f"trend slope {slope:+.3f}")
    else:
        trend_ok = last > sma200 if long else last < sma200
        notes.append(f"close {last:.4f} vs SMA200 {sma200:.4f}")
        if gate == "sma200_and_spy":
            if spy_bars is None:
                raise ValueError("trend_gate sma200_and_spy needs spy_bars")
            spy_close = _closes(spy_bars, "spy_bars")
            _same_session(bars, spy_bars)
            spy_last = float(spy_close[-1])
            spy_sma200 = float(spy_close[-SMA_TREND:].mean())
            spy_ok = spy_last > spy_sma200 if long else spy_last < spy_sma200
            trend_ok = trend_ok and spy_ok
            notes.append(f"SPY {spy_last:.4f} vs SMA200 {spy_sma200:.4f}")
    notes.append(f"trend {'ok' if trend_ok else 'not ok'} for {direction}")
    notes.append(f"RSI{period} {rsi:.2f}")

    structure_state = None
    if exit_mode == "sma5_or_choch":
        if window is None:
            window = _window(bars, close)
        code = _measured(compute_chart_structure(*window).structure_state, "the swing structure")
        if code is not None:
            if code not in _STATE_NAMES:
                raise ValueError(f"Unknown structure_state code {code!r}")
            structure_state = _STATE_NAMES[code]
        notes.append(f"structure {structure_state or 'unmeasurable'}")

    extreme = rsi < threshold if long else rsi > 100.0 - threshold
    if trend_ok and extreme:
        action = "entry"
    else:
        beyond_sma5 = last > sma5 if long else last < sma5
        if exit_mode == "sma5":
            exiting = beyond_sma5
            notes.append(f"close vs SMA5 {sma5:.4f}")
        elif exit_mode == "rsi":
            exiting = rsi > rsi_exit if long else rsi < 100.0 - rsi_exit
            notes.append(f"RSI exit level {rsi_exit:g}")
        elif exit_mode == "sma5_or_choch":
            exiting = beyond_sma5 or structure_state == ("bear" if long else "bull")
            notes.append(f"close vs SMA5 {sma5:.4f}")
        else:  # time: the max-hold rule owns the exit
            exiting = False
        action = "exit" if exiting else "none"

    return {"action": action, "rsi": rsi, "sma200": sma200, "sma5": sma5,
            "trend_ok": bool(trend_ok), "structure_state": structure_state,
            "reason": f"{action}: " + "; ".join(notes)}
