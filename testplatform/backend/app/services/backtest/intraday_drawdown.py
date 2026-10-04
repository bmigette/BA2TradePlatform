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


def estimate_worst_structure_pnl(
    legs: List[Dict[str, Any]],
    entry_underlying_price: float,
    bars_5m: List[Dict[str, Optional[float]]],
    commission_per_leg: float,
) -> Optional[float]:
    """Worst-case P&L (dollars, commission-inclusive) of a MULTI-LEG structure over ``bars_5m``.

    Every leg is re-priced on the SAME underlying print -- each bar's Low and High, exactly the
    candidates ``estimate_worst_intraday_pnl`` uses -- with its own delta, entry premium, sign
    and size, and the legs are SUMMED per print before the minimum is taken over prints. That is
    the whole point: the hedging legs move together, so a short wing is no longer priced as a
    naked short (a per-leg pass read a butterfly worth at most $780 of loss as -$7,608).

    ``legs`` carry ``entry_premium``, ``delta``, ``size``, ``multiplier``, ``direction_sign``.
    Each leg's implied premium is floored at zero, like the single-leg estimate. Commission is
    charged once per leg, like the per-leg rows it replaces. Returns None when there is no
    usable print.
    """
    if not bars_5m:
        return None
    worst: Optional[float] = None
    for bar in bars_5m:
        for px in (bar.get("Low"), bar.get("High")):
            if px is None:
                continue
            total = 0.0
            for leg in legs:
                implied = max(leg["entry_premium"]
                              + leg["delta"] * (float(px) - entry_underlying_price), 0.0)
                total += ((implied - leg["entry_premium"]) * leg["size"] * leg["multiplier"]
                          * leg["direction_sign"])
            total -= commission_per_leg * len(legs)
            if worst is None or total < worst:
                worst = total
    return worst


def structure_max_loss(rows: List[Dict[str, Any]]):
    """``(state, amount)`` -- the structure's TRUE maximum loss in dollars at expiry, from the
    shared payoff evaluator (``option_payoff.max_loss``) over the rows' own fills.

    ``state`` is ``"MEASURED"`` (``amount`` is positive dollars), ``"UNBOUNDED"`` (a naked short
    call or ratio wing: there is NO cap, ``amount`` None) or ``"UNMEASURABLE"`` (legs the
    evaluator refuses; ``amount`` None). Never a finite number for an unbounded structure.
    """
    from ba2_common.core.option_payoff import MEASURED, PayoffLeg, max_loss
    from ba2_common.core.types import OrderDirection

    legs = [PayoffLeg(kind=str(r.get("option_type")),
                      side=OrderDirection.BUY if r.get("direction") == "buy" else OrderDirection.SELL,
                      premium=r["entry_price"], strike=r.get("strike"),
                      ratio=r["size"], multiplier=r["multiplier"])
            for r in rows]
    result = max_loss(legs)
    return result.state, (result.amount if result.state == MEASURED else None)


def _structure_groups(trades: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group trade rows into STRUCTURES, in first-appearance order.

    THE KEY IS ``(transaction_id, entry_time)``. One backtest ``Transaction`` is one entered
    structure and every leg row it produced carries its id (measured: 273 rows -> 91 transactions
    on the O_BF genome, three legs each), but a transaction can also outlive its first package
    (a roll or a re-entry adds legs LATER); legs opened together by one order share the entry
    timestamp, later ones do not, and a group is only priceable as one static package if it
    entered as one. ``option_strategy`` and ``entry_record.structure`` exist on the FIRST leg row
    only, so they cannot identify the structure; ``contract_symbol`` identifies a leg.
    A row without a ``transaction_id`` (hand-built dicts, equity-style rows) is its own group.
    """
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for i, t in enumerate(trades):
        tid = t.get("transaction_id")
        key = ("row", i) if tid is None else (tid, t.get("entry_time"))
        groups.setdefault(key, []).append(t)
    return list(groups.values())


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
    total account wipeout) while the actual equity curve's peak-to-trough was only -41%.

    EACH DIP is floored at -100% (an estimate cannot lose more than the equity it is measured
    on); the INPUT is never clamped. On a capped run the daily figure can legitimately sit
    below -100% (a $30k loss on a $20k cap is -150%), and flooring the result would IMPROVE a
    figure this layer may only ever worsen -- with no flagged trade at all.

    CANDIDATES ARE PER STRUCTURE, NOT PER LEG. A trade row is one LEG; a butterfly or spread is
    several rows. Pricing each row alone read a short body or wing leg as a NAKED short with the
    hedging legs ignored (a butterfly that could lose at most $780 produced a $7,608 "worst
    leg", -40.3% reported against a -12.9% daily curve). Rows are grouped by
    ``_structure_groups``; a single-leg group is priced exactly as before, a multi-leg group by
    ``estimate_worst_structure_pnl`` (all legs on the same 5-minute prints, summed per print) and
    capped at ``structure_max_loss`` -- the structure's true worst payoff, which is NOT the net
    debit for e.g. a call butterfly with a wider upper wing. An unbounded structure has no cap,
    only the summed-legs estimate. A structure missing any leg's delta, or whose legs did not
    enter and exit together, is UNCOVERED as a whole (counted), never priced from a partial leg
    set.

    KNOWN LIMIT: a multi-day trade is measured from the peak at its ENTRY. If the equity sets a
    new peak while the trade is open and the intraday low comes after it, the real dip is
    deeper than this estimate (understated, never overstated).
    """
    if drawdown_base is not None and not drawdown_base > 0:
        # A cap is validated positive at config time (equity_cap.validate_equity_cap); a
        # non-positive base here is a wiring bug, and dividing by it would flip or blow up
        # every dip. Refused for the whole call, not skipped trade by trade in the loop below.
        raise ValueError(f"drawdown_base must be > 0 or None, got {drawdown_base!r}")
    refined = max_drawdown
    flagged = 0        # trades/structures that reached the delta lookup (already past the dip filter)
    uncovered = 0      # of those, the ones with no usable entry delta / underlying price
    errored = 0        # option trades dropped because a lookup or the arithmetic raised
    no_base = 0        # dips dropped because their denominator (the peak) was not positive
    structures = 0     # flagged MULTI-LEG structures (a subset of ``flagged``)
    staggered = 0      # of those, uncovered because their legs did not enter/exit as one package
    capped = 0         # structures whose estimate was bounded by their true max loss
    uncapped_unbounded = 0     # structures with unbounded loss: no cap, summed-legs estimate only
    uncapped_unmeasurable = 0  # structures the payoff evaluator refused: no cap (loud)

    def _lay_dip(entry_time: Any, worst_loss: float) -> None:
        """Lay a worst loss on the equity at ``entry_time`` and fold its dip into ``refined``."""
        nonlocal refined, no_base
        equity = equity_at(entry_time)
        peak = peak_at(entry_time)
        if not equity or peak is None:
            return
        # A running peak includes the point it is read at, so it can never sit below the
        # equity read there; a source that says otherwise would make the dip shallower (or
        # positive) and hide exactly the risk this layer exists to surface.
        peak = max(float(peak), float(equity))
        base = drawdown_base if drawdown_base is not None else peak
        if base <= 0:
            no_base += 1
            return
        dip_dd = max((float(equity) + worst_loss - peak) / base * 100.0, -100.0)
        refined = min(refined, dip_dd)

    def _single_leg(t: Dict[str, Any]) -> None:
        """One option leg priced on its own -- the pre-structure behaviour, unchanged."""
        nonlocal flagged, uncovered
        contract = t.get("contract_symbol")
        underlying = t.get("underlying_symbol")
        exit_low = daily_bar_low(underlying, t.get("exit_time"))
        prior_low = prior_daily_bar_low(underlying, t.get("exit_time"))
        if not is_flagged_for_intraday_check(t, prior_low, exit_low):
            return
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
            return
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
            return
        worst_loss = min(0.0, worst_pnl)
        if worst_loss == 0.0:
            # The window never went below entry: equity_at(entry) is itself a point on the
            # daily curve, which max_drawdown already covers.
            return
        _lay_dip(t.get("entry_time"), worst_loss)

    def _structure(rows: List[Dict[str, Any]]) -> None:
        """A multi-leg structure priced AS ONE: all legs on the same underlying path, summed per
        print, capped at the structure's true maximum loss. Never priced from a partial leg
        set: any leg without a delta, or a package whose legs did not enter and exit together,
        leaves the WHOLE structure uncovered (counted)."""
        nonlocal flagged, uncovered, structures, staggered, capped
        nonlocal uncapped_unbounded, uncapped_unmeasurable
        # Flagged if ANY leg's exit flags it: the legs of one package exit together, so in
        # practice they agree; "any" cannot hide a dip one of them shows.
        any_flag = False
        for t in rows:
            exit_low = daily_bar_low(t["underlying_symbol"], t.get("exit_time"))
            prior_low = prior_daily_bar_low(t["underlying_symbol"], t.get("exit_time"))
            if is_flagged_for_intraday_check(t, prior_low, exit_low):
                any_flag = True
        if not any_flag:
            return
        flagged += 1
        structures += 1
        underlying = rows[0]["underlying_symbol"]
        entry_time = rows[0].get("entry_time")
        exit_time = rows[0].get("exit_time")
        one_package = (
            len({t["underlying_symbol"] for t in rows}) == 1
            and len({t["contract_symbol"] for t in rows}) == len(rows)
            and len({t.get("exit_time") for t in rows}) == 1)
        if not one_package:
            uncovered += 1
            staggered += 1
            return
        entry_underlying_px = underlying_price_at(underlying, entry_time)
        deltas = [delta_at_entry(underlying, t["contract_symbol"], entry_time) for t in rows]
        if entry_underlying_px is None or any(d is None for d in deltas):
            uncovered += 1
            return
        bars = bars_5m_between(underlying, entry_time, exit_time)
        worst_pnl = estimate_worst_structure_pnl(
            [{"entry_premium": t["entry_price"], "delta": d, "size": t["size"],
              "multiplier": multiplier,
              "direction_sign": 1.0 if t.get("direction") == "buy" else -1.0}
             for t, d in zip(rows, deltas)],
            entry_underlying_px, bars, commission_per_trade)
        if worst_pnl is None:
            return
        state, amount = structure_max_loss(rows)
        if amount is not None:
            # The estimate is first-order; the structure's payoff is exact. It cannot lose
            # more than its worst payoff plus the commissions it paid.
            floor = -(amount + commission_per_trade * len(rows))
            if worst_pnl < floor:
                worst_pnl = floor
                capped += 1
        elif state == "UNBOUNDED":
            uncapped_unbounded += 1      # no cap exists: the summed-legs estimate stands alone
        else:
            uncapped_unmeasurable += 1
        worst_loss = min(0.0, worst_pnl)
        if worst_loss == 0.0:
            return
        _lay_dip(entry_time, worst_loss)

    for group in _structure_groups(trades):
        option_rows = [t for t in group if t.get("contract_symbol") and t.get("underlying_symbol")]
        if not option_rows:
            continue
        # One distinct contract is ONE leg (possibly several fills of it): priced per row
        # exactly as before, which is what keeps every single-leg run bit-identical.
        singles = len({t["contract_symbol"] for t in option_rows}) == 1
        try:
            if singles:
                for t in option_rows:
                    try:
                        _single_leg(t)
                    except Exception as e:  # noqa: BLE001 - best-effort per trade; counted below
                        errored += 1
                        logger.debug(f"intraday drawdown refinement skipped for a trade: "
                                     f"{type(e).__name__}: {e}")
            else:
                _structure(option_rows)
        except Exception as e:  # noqa: BLE001 - best-effort per structure; counted below
            errored += 1
            logger.debug(f"intraday drawdown refinement skipped for a structure: "
                         f"{type(e).__name__}: {e}")
    if flagged or errored:
        pct = uncovered / flagged * 100.0 if flagged else 0.0
        # WARNING, not debug, past a third: the refinement moved from the entry day's own
        # snapshot to the last one strictly BEFORE it, so a contract whose first snapshot IS
        # its entry day now has no prior and drops out. That is the correct answer, but it
        # changes what the refined figure covers, and coverage is not visible in the result.
        # Any trade dropped by an exception or a non-positive base is a WARNING too: those
        # are not "no dip", they are trades the figure silently does not cover.
        loud = pct >= 33.0 or errored > 0 or no_base > 0 or uncapped_unmeasurable > 0
        msg = (f"intraday drawdown refinement: {flagged - uncovered}/{flagged} flagged trade(s) "
               f"had a usable pre-entry delta ({pct:.0f}% uncovered); {errored} trade(s) skipped "
               f"on an exception, {no_base} on a non-positive drawdown base")
        if structures:
            msg += (f"; {structures} multi-leg structure(s) priced as one ({capped} capped at "
                    f"their max loss, {uncapped_unbounded} unbounded/uncapped, "
                    f"{uncapped_unmeasurable} uncapped as unmeasurable, {staggered} uncovered "
                    f"because their legs did not enter and exit together)")
        (logger.warning if loud else logger.info)(msg)
    # Only the dips were floored; ``refined`` starts at the input and only ever moves down.
    return refined
