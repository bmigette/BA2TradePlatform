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
and re-price the option there. The trade's worst point is then read as a DIP from the running
equity peak at its entry; if that dip is deeper than the daily curve's worst, it becomes
``max_drawdown``.

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
    peak_at: Callable[[Any], Optional[float]],
    daily_bar_low: Callable[[str, Any], Optional[float]],
    prior_daily_bar_low: Callable[[str, Any], Optional[float]],
    delta_at_entry: Callable[[str, str, Any], Optional[float]],
    underlying_price_at: Callable[[str, Any], Optional[float]],
    bars_5m_between: Callable[[str, Any, Any], List[Dict[str, Optional[float]]]],
    commission_per_trade: float = 0.0,
    multiplier: float = 100.0,
    drawdown_base: Optional[float] = None,
) -> float:
    """Re-derive ``max_drawdown`` (percentage points, <= 0), folding in an estimated intraday
    dip for each flagged option trade. All data access is dependency-injected so this stays
    testable without real cache files / a live account -- see the module docstring. Only ever
    makes the result MORE negative (a worse drawdown) than the input; never improves on the
    daily-computed figure, since the daily curve is authoritative and this is a refinement on
    top of it, not a replacement.

    A TRADE'S CANDIDATE IS A DIP, NOT A P&L DIFFERENCE. The trade's estimated worst intraday
    P&L is laid on top of the equity it opened on and measured from the running peak at that
    point, exactly as the daily curve measures every other point::

        dip_dd = (equity_at(entry) + min(0, worst_pnl) - peak) / base * 100

    where ``peak = max(peak_at(entry), equity_at(entry))`` and ``base`` is ``peak`` (the
    running-peak drawdown of ``results._drawdown_curve``) or, on an equity-capped run, the
    fixed ``drawdown_base`` (``equity_cap.capped_drawdown_curve`` divides by the cap). The
    realised P&L plays no part. This replaced ``max_drawdown + (worst_pnl - realised_pnl) /
    equity``, which counted a WINNING trade's realised gain as drawdown: +$51,048 realised
    against a -$500 intraday worst read as a -$51,548 "extra loss", -462%, floored to -100% --
    the bust sentinel on a profitable run -- and inflated every refined option drawdown (O_LC
    TOP1: -34.27% refined against a -25.49% daily curve).

    Each flagged trade's candidate is computed independently, never on top of the running
    ``refined`` value -- these are separate, non-overlapping trades on different dates
    whose hypothetical worst cases are mutually exclusive (they can't all have happened to the
    SAME equity trough at once). Accumulating them additively across hundreds of trades would
    make the result worsen without bound purely from trade COUNT, not from any single real
    dip -- confirmed live: a 251-trade run's refined drawdown reached -101.71% (worse than a
    total account wipeout) while the actual equity curve's peak-to-trough was only -41%. The
    result is also hard-floored at -100%: drawdown relative to total equity cannot exceed that
    even as a hypothetical estimate.
    """
    if drawdown_base is not None and not drawdown_base > 0:
        # A cap is validated positive at config time (equity_cap.validate_equity_cap); a
        # non-positive base here is a wiring bug, and dividing by it would flip or blow up
        # every dip. Refused for the whole call, not skipped trade by trade in the loop below.
        raise ValueError(f"drawdown_base must be > 0 or None, got {drawdown_base!r}")
    refined = max_drawdown
    flagged = 0        # trades that reached the delta lookup (already past the dip filter)
    uncovered = 0      # of those, the ones with no usable entry delta / underlying price
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
            flagged += 1
            delta = delta_at_entry(underlying, contract, t.get("entry_time"))
            entry_underlying_px = underlying_price_at(underlying, t.get("entry_time"))
            if delta is None or entry_underlying_px is None:
                # UNCOVERED, not "no dip". Skipping is right -- a missing delta cannot be
                # coerced to 0.0, which would claim the premium does not move with the
                # underlying and silently report the daily drawdown as refined. But a run
                # where most flagged trades are uncovered has a refined figure that means
                # something different from one where all of them were, and nothing in the
                # result could show that. Counted and reported below.
                uncovered += 1
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
            worst_loss = min(0.0, worst_pnl)
            if worst_loss == 0.0:
                # The window never went below entry: equity_at(entry) is itself a point on the
                # daily curve, which max_drawdown already covers.
                continue
            equity = equity_at(t.get("entry_time"))
            peak = peak_at(t.get("entry_time"))
            if not equity or peak is None:
                continue
            # A running peak includes the point it is read at, so it can never sit below the
            # equity read there; a source that says otherwise would make the dip shallower (or
            # positive) and hide exactly the risk this layer exists to surface.
            peak = max(float(peak), float(equity))
            base = drawdown_base if drawdown_base is not None else peak
            if base <= 0:
                continue
            dip_dd = (float(equity) + worst_loss - peak) / base * 100.0
            refined = min(refined, dip_dd)
        except Exception as e:  # noqa: BLE001 - best-effort refinement, never break the backtest
            logger.debug(f"intraday drawdown refinement skipped for a trade: {e}")
            continue
    if flagged:
        pct = uncovered / flagged * 100.0
        # WARNING, not debug, past a third: the refinement moved from the entry day's own
        # snapshot to the last one strictly BEFORE it, so a contract whose first snapshot IS
        # its entry day now has no prior and drops out. That is the correct answer, but it
        # changes what the refined figure covers, and coverage is not visible in the result.
        (logger.warning if pct >= 33.0 else logger.info)(
            f"intraday drawdown refinement: {flagged - uncovered}/{flagged} flagged trade(s) "
            f"had a usable pre-entry delta ({pct:.0f}% uncovered)")
    return max(refined, -100.0)
