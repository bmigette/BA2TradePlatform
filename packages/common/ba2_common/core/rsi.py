"""Wilder RSI, the one implementation shared by the PullbackReversion expert and the RSI rule
condition (``TradeConditions.RsiCondition``)."""
from __future__ import annotations

import numpy as np


def wilder_rsi(close: np.ndarray, period: int) -> float:
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
