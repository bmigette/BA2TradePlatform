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


def deployed_equity(real_equity: Optional[float],
                    *, cap: Optional[float]) -> Optional[float]:
    """The equity the SIZER may see: ``min(cap, real_equity)``.

    ``real_equity`` includes unrealised marks, deliberately: an open position that is down
    genuinely leaves less to deploy, and one that is up is exactly the excess the cap withholds.

    ``None`` in, ``None`` out -- an engine that cannot state its equity has not stated zero.
    """
    if real_equity is None:
        return None
    if cap is None:
        return real_equity
    return min(float(cap), float(real_equity))


def scoring_curve(equity_curve: Sequence[Dict[str, Any]],
                  *, cap: Optional[float]) -> List[Dict[str, Any]]:
    """Restate a REAL equity curve on a fixed denominator, for the metrics.

    Each period's return is ``period_pnl / cap`` -- the CAP, never the running equity -- and the
    returns are compounded. $5,000 a year on a $20,000 cap therefore reads 25% every year rather
    than 25 / 20 / 16.7 / 14.3, and a steady strategy scores the same whatever year it started.

    ``equity_curve`` must be the REAL recorded series. Differencing the capped figure would report
    zero P&L for every period spent above the cap.

    A "period" is one point of the recorded curve -- whatever granularity ``snapshot_equity``
    wrote. No resampling: a second time base would let this curve and the trade ledger disagree
    about when a return happened.
    """
    if cap is None:
        return list(equity_curve)
    pts = list(equity_curve)
    if not pts:
        return []
    out = [{**pts[0], "equity": float(cap)}]
    level = float(cap)
    for prev, cur in zip(pts, pts[1:]):
        period_pnl = float(cur["equity"]) - float(prev["equity"])
        level *= (1.0 + period_pnl / float(cap))
        out.append({**cur, "equity": level})
    return out


def capped_drawdown_curve(equity_curve: Sequence[Dict[str, Any]],
                          *, cap: Optional[float]) -> List[Dict[str, Any]]:
    """Peak-to-trough on cumulative P&L, divided by the CAP.

    A $2,000 trough is 10% of a $20,000 cap whenever it happens. On the compounded scoring curve
    it would read -10% in year one and -5% in year four -- risk on a moving denominator while
    returns sit on a fixed one, which hands a late-run strategy a better ``dd_guard`` multiplier
    for no reason but arithmetic.
    """
    pts = list(equity_curve)
    if not pts:
        return []
    if cap is None:
        raise EquityCapError(
            "capped_drawdown_curve called with cap=None; use results._drawdown_curve instead")
    base = float(pts[0]["equity"])
    peak_pnl = 0.0
    out: List[Dict[str, Any]] = []
    for pt in pts:
        pnl = float(pt["equity"]) - base
        peak_pnl = max(peak_pnl, pnl)
        out.append({"date": pt["date"], "drawdown": (pnl - peak_pnl) / float(cap) * 100.0})
    return out
