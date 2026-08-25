"""The optional fixed-notional equity cap for a backtest.

A backtest compounds, so a strategy that did well in year one deploys larger positions ever
after and its later results are carried by its earlier luck. This holds the capital still, so a
result is about the strategy rather than about when it started.

TWO QUANTITIES LIVE HERE AND MUST NOT BE CONFLATED:

  deployed_equity()  -- what the SIZER, buying power, margin and the option rails may see.
                        Capped. Never reaches the recorded equity curve.
  scoring_curve()    -- what the METRICS see. Built from the REAL recorded equity, with every
                        period's return divided by the FIXED cap so a steady strategy reads the
                        same percentage every year.

Feeding the capped figure into scoring would report zero P&L for every period spent above the
cap -- the strategy would appear to stop earning the moment it succeeded.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence


class EquityCapError(ValueError):
    """A configured equity cap that cannot be honoured. Raised at config time, never mid-run."""


def validate_equity_cap(
    raw: Any,
    *,
    initial_capital: Optional[float] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[float]:
    """Normalise a configured cap. ``None`` means the feature is off.

    A cap ABOVE the initial capital is allowed -- it cannot bind yet, but the account may grow
    into it. That is a fact worth logging, not an error.
    """
    if raw is None:
        return None
    # bool is an int subclass; a boolean in a money field is a caller bug, not a $1 cap.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise EquityCapError(
            f"equity_cap must be a number or None, got {type(raw).__name__}: {raw!r}")
    cap = float(raw)
    if not math.isfinite(cap):
        raise EquityCapError(f"equity_cap must be finite, got {cap!r}")
    if cap <= 0:
        raise EquityCapError(
            f"equity_cap must be greater than zero, got {cap:,.2f}. A cap of zero would make "
            f"every position unaffordable; omit the setting to disable the feature.")
    if initial_capital is not None and cap > float(initial_capital):
        if log is not None:
            log(f"equity_cap {cap:,.2f} is above the initial capital "
                f"{float(initial_capital):,.2f}, so it cannot bind until the account grows to it.")
    return cap
