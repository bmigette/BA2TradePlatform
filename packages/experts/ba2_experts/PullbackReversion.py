"""Short-horizon pullback reversion: buy an oversold dip in an uptrend (long), or sell an
overbought rally in a downtrend (short). Research-only.

``pullback_signal`` is the pure, causal decision for the LAST bar of a completed daily history.
Its semantics mirror the feasibility probe ``test_files/pullback_feasibility_20260924.py``
(``rsi``, ``entry_ok``, ``exit_signal``); the market-condition gates read the platform's own
calculators over the 128-bar window ending at that bar, exactly as the live gate would.
Missing, short or invalid inputs raise ``ValueError``: the function never answers "none" for a
bar it could not evaluate.
"""
from __future__ import annotations

import math
from numbers import Real
from typing import Mapping, Optional

import pandas as pd

from ba2_common.core.market_conditions import (
    STATUS_VALID, STRUCTURE_STATE_CODES, STRUCTURE_STATE_NONE_CODE, WINDOW,
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


def _closes(bars, name: str) -> pd.Series:
    """The validated close series of a completed daily history (ascending, unique sessions)."""
    if not isinstance(bars, pd.DataFrame):
        raise ValueError(f"{name} must be a DataFrame, got {type(bars).__name__}")
    missing = [k for k in _OHLCV if k not in bars.columns]
    if missing:
        raise ValueError(f"{name} is missing columns {missing}")
    if len(bars) < MIN_BARS:
        raise ValueError(f"Insufficient history in {name}: {len(bars)} bars, need {MIN_BARS}")
    if not (bars.index.is_monotonic_increasing and bars.index.is_unique):
        raise ValueError(f"{name} index must be strictly ascending by session")
    close = pd.to_numeric(bars["Close"], errors="raise").astype(float)
    last = float(close.iloc[-1])
    if not (math.isfinite(last) and last > 0):
        raise ValueError(f"Invalid final close in {name} at {bars.index[-1]}: {last!r}")
    bad = ~(close.map(math.isfinite) & (close > 0))
    if bad.any():
        raise ValueError(f"Invalid close in {name} at {close.index[bad.to_numpy()][0]}")
    return close


def wilder_rsi(close: pd.Series, period: int) -> float:
    """Wilder RSI of the last bar, as the probe's ``rsi()``: ``ewm(alpha=1/n, adjust=False)``.

    With no down move in the smoothed history the RSI is 100 (the probe yields NaN there, which
    no caller can act on); with no move at all it is undefined and raises."""
    delta = close.diff()
    up = float(delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean().iloc[-1])
    down = float((-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean().iloc[-1])
    if down == 0:
        if up == 0:
            raise ValueError("RSI undefined: no price change in the history")
        return 100.0
    return 100.0 - 100.0 / (1.0 + up / down)


def _window(bars: pd.DataFrame):
    tail = bars.iloc[-WINDOW:]
    return tuple(tail[k].to_numpy(dtype=float) for k in _OHLCV)


def pullback_signal(bars: pd.DataFrame, settings: Mapping,
                    spy_bars: Optional[pd.DataFrame] = None) -> dict:
    """Entry / exit / none for the last bar of ``bars`` (completed daily OHLCV, ascending).

    ``spy_bars`` is required by ``trend_gate == "sma200_and_spy"`` only; it must end on the same
    session as ``bars``. Entry beats exit on the same bar; ``exit_mode == "time"`` never signals
    (the max-hold rule owns that exit)."""
    direction = _choice(settings, "direction", DIRECTIONS)
    gate = _choice(settings, "trend_gate", TREND_GATES)
    period = _number(settings, "rsi_period", 2, 5, integer=True)
    threshold = _number(settings, "entry_threshold", 1.0, 30.0)
    exit_mode = _choice(settings, "exit_mode", EXIT_MODES)
    rsi_exit = _number(settings, "rsi_exit", 50.0, 90.0)
    long = direction == "long"

    close = _closes(bars, "bars")
    last = float(close.iloc[-1])
    sma200 = float(close.iloc[-SMA_TREND:].mean())
    sma5 = float(close.iloc[-SMA_EXIT:].mean())
    rsi = wilder_rsi(close, period)
    notes = []

    if gate == "slope_ohlcv_v1":
        slope = compute_market_conditions(*_window(bars)).trend_slope
        if slope.status != STATUS_VALID:
            trend_ok = False
            notes.append(f"trend slope unknown ({slope.status}: {slope.reason})")
        else:
            trend_ok = slope.value > 0 if long else slope.value < 0
            notes.append(f"trend slope {slope.value:+.3f}")
    else:
        trend_ok = last > sma200 if long else last < sma200
        notes.append(f"close {last:.4f} vs SMA200 {sma200:.4f}")
        if gate == "sma200_and_spy":
            if spy_bars is None:
                raise ValueError("trend_gate sma200_and_spy needs spy_bars")
            spy_close = _closes(spy_bars, "spy_bars")
            if spy_bars.index[-1] != bars.index[-1]:
                raise ValueError(f"spy_bars end on {spy_bars.index[-1]}, bars on {bars.index[-1]}: "
                                 "the market regime must be read on the decision session")
            spy_last = float(spy_close.iloc[-1])
            spy_sma200 = float(spy_close.iloc[-SMA_TREND:].mean())
            spy_ok = spy_last > spy_sma200 if long else spy_last < spy_sma200
            trend_ok = trend_ok and spy_ok
            notes.append(f"SPY {spy_last:.4f} vs SMA200 {spy_sma200:.4f}")
    notes.append(f"trend {'ok' if trend_ok else 'not ok'} for {direction}")

    extreme = rsi < threshold if long else rsi > 100.0 - threshold
    notes.append(f"RSI{period} {rsi:.2f}")
    structure_state = None
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
            state = compute_chart_structure(*_window(bars)).structure_state
            if state.status == STATUS_VALID:
                if state.value not in _STATE_NAMES:
                    raise ValueError(f"Unknown structure_state code {state.value!r}")
                structure_state = _STATE_NAMES[state.value]
                notes.append(f"structure {structure_state}")
            else:
                notes.append(f"structure unknown ({state.status}: {state.reason})")
            against = "bear" if long else "bull"
            exiting = beyond_sma5 or structure_state == against
            notes.append(f"close vs SMA5 {sma5:.4f}")
        else:  # time: the max-hold rule owns the exit
            exiting = False
        action = "exit" if exiting else "none"

    return {"action": action, "rsi": rsi, "sma200": sma200, "sma5": sma5,
            "trend_ok": bool(trend_ok), "structure_state": structure_state,
            "reason": f"{action}: " + "; ".join(notes)}
