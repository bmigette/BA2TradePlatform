"""Post-hoc drawdown refinement for options trades whose realised P&L was computed from DAILY
option-premium bars only (see ``options_cache.py``'s ``option_bar`` table -- no intraday premium
data exists, cache is one row per contract per DATE). Two categories of trade can hide a real
intraday drawdown that the close-to-close equity curve never captures:

  * trades held for a single bar (``bars_held <= 1``) -- the position never got a SECOND daily
    snapshot while open, so ANY adverse intraday move within that one day is invisible;
  * trades whose EXIT bar's underlying low undercuts the prior bar's low -- a signal that a real
    intraday down-move happened on the way to the close, which a close-only reading smooths over
    even for a trade that closed profitably.

For flagged trades, this estimates a WORSE-case intraday premium using the underlying's
5-minute bars (real, cached data -- see ``FMPOHLCVProvider``'s ``*_5min.parquet``) and the
option's delta as-of entry (real, cached data -- see ``options_cache.py``'s ``option_chain``
table): assuming premium moves linearly with delta x underlying price change (a first-order
approximation -- it ignores gamma/theta/vega, so it is directionally useful, not a precise
recomputation), find the underlying's most adverse 5-minute print in the trade's holding window
and re-price the option there. If that implies a bigger drawdown than what the daily curve
recorded, fold it into ``max_drawdown``.

All data access is dependency-injected (callables) so the estimation math is unit-testable
without real cache files or a live account -- see ``_build_refine_callbacks`` in ``results.py``
for the real wiring. ``delta_at_entry`` takes ``(underlying, contract, entry_time)`` since the
options chain cache is keyed by (underlying, as_of), not by contract alone.

Best-effort throughout: missing 5m data, missing delta, or any lookup failure for a given trade
silently skips that trade's refinement (falls back to the daily-only figure) rather than failing
the backtest -- this is a REFINEMENT layer on top of the authoritative daily engine, never a hard
dependency of it.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ba2_common.logger import logger


def is_flagged_for_intraday_check(
    trade: Dict[str, Any],
    prior_bar_low: Optional[float],
    exit_bar_low: Optional[float],
) -> bool:
    """True if this trade's realised P&L might be hiding a real intraday drawdown."""
    if (trade.get("bars_held") or 0) <= 1:
        return True
    if prior_bar_low is not None and exit_bar_low is not None:
        return exit_bar_low < prior_bar_low
    return False


def estimate_worst_intraday_pnl(
    entry_premium: float,
    entry_underlying_price: float,
    delta: float,
    size: float,
    multiplier: float,
    commission: float,
    bars_5m: List[Dict[str, Optional[float]]],
    direction_sign: float,
) -> Optional[float]:
    """Worst-case P&L (dollars, commission-inclusive) implied by the underlying's 5-minute bars
    during the holding window, re-pricing the premium linearly off delta at each bar's high AND
    low. Checking both per bar (rather than assuming call-vs-put) needs no ``option_type``
    input -- delta's own sign already determines which side is adverse for a LONG option; for a
    SHORT option the adverse direction flips, handled here via ``direction_sign`` (+1 long
    /-1 short) picking the min vs. max implied premium across the window.

    Returns None if there's no usable 5-minute data (the caller should then leave the trade's
    already-recorded (daily-only) drawdown contribution untouched).
    """
    if not bars_5m:
        return None
    implied_prices: List[float] = []
    for bar in bars_5m:
        for px in (bar.get("Low"), bar.get("High")):
            if px is None:
                continue
            implied = entry_premium + delta * (float(px) - entry_underlying_price)
            implied_prices.append(max(implied, 0.0))  # premium can't go negative
    if not implied_prices:
        return None
    # LONG (direction_sign > 0) loses when premium DROPS -> the adverse extreme is the MIN
    # implied premium. SHORT (direction_sign < 0) loses when premium RISES -> the MAX.
    worst_premium = min(implied_prices) if direction_sign > 0 else max(implied_prices)
    return (worst_premium - entry_premium) * size * multiplier * direction_sign - commission


def refine_max_drawdown(
    trades: List[Dict[str, Any]],
    max_drawdown: float,
    *,
    equity_at: Callable[[Any], Optional[float]],
    daily_bar_low: Callable[[str, Any], Optional[float]],
    prior_daily_bar_low: Callable[[str, Any], Optional[float]],
    delta_at_entry: Callable[[str, str, Any], Optional[float]],
    underlying_price_at: Callable[[str, Any], Optional[float]],
    bars_5m_between: Callable[[str, Any, Any], List[Dict[str, Optional[float]]]],
    commission_per_trade: float = 0.0,
    multiplier: float = 100.0,
) -> float:
    """Re-derive ``max_drawdown`` (percentage points, <= 0), folding in an estimated intraday
    dip for each flagged option trade. All data access is dependency-injected so this stays
    testable without real cache files / a live account -- see the module docstring. Only ever
    makes the result MORE negative (a worse drawdown) than the input; never improves on the
    daily-computed figure, since the daily curve is authoritative and this is a refinement on
    top of it, not a replacement.

    Each flagged trade's candidate is computed against the ORIGINAL ``max_drawdown``, not the
    running ``refined`` value -- these are separate, non-overlapping trades on different dates
    whose hypothetical worst cases are mutually exclusive (they can't all have happened to the
    SAME equity trough at once). Accumulating them additively across hundreds of trades would
    make the result worsen without bound purely from trade COUNT, not from any single real
    dip -- confirmed live: a 251-trade run's refined drawdown reached -101.71% (worse than a
    total account wipeout) while the actual equity curve's peak-to-trough was only -41%. The
    result is also hard-floored at -100%: drawdown relative to total equity cannot exceed that
    even as a hypothetical estimate.
    """
    refined = max_drawdown
    for t in trades:
        contract = t.get("contract_symbol")
        underlying = t.get("underlying_symbol")
        if not contract or not underlying:
            continue
        try:
            exit_low = daily_bar_low(underlying, t.get("exit_time"))
            prior_low = prior_daily_bar_low(underlying, t.get("exit_time"))
            if not is_flagged_for_intraday_check(t, prior_low, exit_low):
                continue
            delta = delta_at_entry(underlying, contract, t.get("entry_time"))
            entry_underlying_px = underlying_price_at(underlying, t.get("entry_time"))
            if delta is None or entry_underlying_px is None:
                continue
            bars = bars_5m_between(underlying, t.get("entry_time"), t.get("exit_time"))
            direction_sign = 1.0 if t.get("direction") == "buy" else -1.0
            worst_pnl = estimate_worst_intraday_pnl(
                entry_premium=t["entry_price"],
                entry_underlying_price=entry_underlying_px,
                delta=delta,
                size=t["size"],
                multiplier=multiplier,
                commission=commission_per_trade,
                bars_5m=bars,
                direction_sign=direction_sign,
            )
            if worst_pnl is None:
                continue
            realised_pnl = t.get("pnl") or 0.0
            extra_loss = min(0.0, worst_pnl - realised_pnl)  # only matters if it's WORSE
            if extra_loss == 0.0:
                continue
            equity = equity_at(t.get("entry_time"))
            if not equity:
                continue
            candidate_dd = max_drawdown + (extra_loss / equity * 100.0)
            refined = min(refined, candidate_dd)
        except Exception as e:  # noqa: BLE001 - best-effort refinement, never break the backtest
            logger.debug(f"intraday drawdown refinement skipped for a trade: {e}")
            continue
    return max(refined, -100.0)
