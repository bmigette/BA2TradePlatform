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

import datetime
from typing import Any, Callable, Dict, List, Optional

from ba2_common.core.types import OptionRight
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


class StructureUncovered(Exception):
    """A multi-leg structure that cannot be priced from its legs (no usable iv, rate or
    price). Raised inside the estimate and turned into a COUNTED uncovered structure by the
    caller -- never into a silent fallback to a cruder estimate."""


def _leg_value(leg: Dict[str, Any], spot: float, when: Any, rate: float) -> float:
    """One leg's per-share model value at ``spot`` on ``when``'s date: Black-Scholes through
    the engine's own pricer (``option_bs.bs_price``, the mark-fallback pricer) with the leg's
    entry iv and the run's risk-free rate; intrinsic once the contract is at/after expiry
    (``bs_price``'s DTE=0 convention). Whole days to expiry, like ``_bs_fallback_premium``."""
    from ba2_common.core.option_bs import bs_price

    is_call = leg["right"] == OptionRight.CALL
    day = when.date() if hasattr(when, "date") else when
    dte_days = (leg["expiry"] - day).days
    if dte_days <= 0:
        k = float(leg["strike"])
        return max(spot - k, 0.0) if is_call else max(k - spot, 0.0)
    value = bs_price(float(spot), float(leg["strike"]), dte_days, float(leg["iv"]),
                     leg["right"], r=rate)
    if value is None:
        raise StructureUncovered(
            f"Black-Scholes could not price {leg.get('contract')} (iv={leg['iv']!r}, "
            f"spot={spot!r}, dte={dte_days})")
    return value


def estimate_worst_structure_pnl(
    legs: List[Dict[str, Any]],
    entry_underlying_price: float,
    entry_time: Any,
    bars_5m: List[Dict[str, Optional[float]]],
    commission_per_leg: float,
    rate: float,
) -> Optional[float]:
    """Worst-case P&L (dollars, commission-inclusive) of a MULTI-LEG structure over ``bars_5m``.

    Every leg is re-priced on the SAME underlying price -- over each session's [low, high]
    range, on a grid of it -- with Black-Scholes (the engine's pricer, the leg's entry iv, its
    remaining whole days AT ENTRY, the run's risk-free rate), so GAMMA is in the estimate: a linear-delta sum reads a
    near-delta-neutral short-premium structure (iron condor, strangle) as almost flat when a
    move to its short strike really costs hundreds of dollars. A leg's premium at a print is
    ``entry_premium + (model(print) - model(entry))`` floored at zero: anchoring on the entry
    fill cancels the model's bias, like the linear estimate's ``entry_premium + delta * dS``.
    Legs are SUMMED per print and the minimum over prints taken. Commission is charged once per
    leg. A bar may carry ``Date`` (its session); without one the entry date is used.

    TIME IS FROZEN AT ENTRY (no theta). The question this layer answers is what an adverse
    UNDERLYING move costs beyond the daily curve; the daily curve is built from the real
    marks, which already carry the decay. Letting remaining days shrink along the window
    charged a long structure's whole decay as an extra "dip" (genome B: -15.35% refined
    against -13.85% daily, and -13.85% again with time frozen), and it is the same basis the
    single-leg estimate has always used (delta x spot move, no theta). It errs against short
    premium (which would gain decay), never in its favour.

    ``legs`` carry ``entry_premium``, ``iv``, ``size``, ``multiplier``, ``direction_sign``,
    ``strike``, ``right`` (``OptionRight``), ``expiry`` (``date``). Raises
    ``StructureUncovered`` when any leg cannot be priced; returns None when there is no usable
    print.
    """
    if not bars_5m:
        return None
    anchors = [_leg_value(leg, entry_underlying_price, entry_time, rate) for leg in legs]
    # ONE SESSION AT A TIME. With time frozen at entry the structure's value is a function of
    # the underlying price alone, and the underlying's path is continuous: over a session it passes through EVERY price in [low, high], not only
    # the 5-minute prints. The worst point of that function over the session range is what is
    # wanted, and it is found on a coarse grid of the range -- its two ends, ``_GRID_INTERIOR``
    # equispaced interior points and every leg strike inside it (an interior extremum of a
    # strike-kinked payoff sits near a strike) -- instead of pricing every print of every
    # bar (~150 per session). Measured: the per-print form cost ~470 Black-Scholes calls per
    # structure-day.
    sessions: Dict[Any, List[float]] = {}
    for bar in bars_5m:
        when = bar.get("Date") or entry_time
        day = when.date() if hasattr(when, "date") else when
        for px in (bar.get("Low"), bar.get("High")):
            if px is not None:
                r = sessions.setdefault(day, [float(px), float(px), when])
                r[0] = min(r[0], float(px))
                r[1] = max(r[1], float(px))
    worst: Optional[float] = None
    for lo, hi, when in sessions.values():
        prices = {lo, hi}
        if hi > lo:
            step = (hi - lo) / (_GRID_INTERIOR + 1)
            prices.update(lo + step * i for i in range(1, _GRID_INTERIOR + 1))
            prices.update(float(leg["strike"]) for leg in legs if lo < float(leg["strike"]) < hi)
        for px in prices:
            total = 0.0
            for leg, anchor in zip(legs, anchors):
                implied = max(leg["entry_premium"]
                              + (_leg_value(leg, px, entry_time, rate) - anchor), 0.0)
                total += ((implied - leg["entry_premium"]) * leg["size"] * leg["multiplier"]
                          * leg["direction_sign"])
            total -= commission_per_leg * len(legs)
            if worst is None or total < worst:
                worst = total
    return worst


#: Interior grid points per session range (see ``estimate_worst_structure_pnl``).
_GRID_INTERIOR = 4


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
    iv_at_entry: Optional[Callable[[str, str, Any], Optional[float]]] = None,
    risk_free_rate: Optional[Callable[[Any], float]] = None,
    stats: Optional[Dict[str, int]] = None,
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
    ``_structure_groups``; a single-leg group is priced exactly as before (linear delta), a
    multi-leg group by ``estimate_worst_structure_pnl`` -- every leg re-priced with
    Black-Scholes on the same 5-minute prints (gamma included; ``iv_at_entry`` and
    ``risk_free_rate`` are the seams) and summed per print -- capped at ``structure_max_loss``,
    the structure's true worst payoff, which is NOT the net debit for e.g. a call butterfly
    with a wider upper wing. An unbounded structure has no cap, only the summed estimate.
    A structure with any leg lacking an iv (or a run with no iv / rate seam) is UNCOVERED as a
    whole (counted), never priced from a partial leg set and never from a cruder estimate.
    A structure whose legs exit at different times is priced over the window in which ALL its
    legs are held (entry until the FIRST exit) and counted as ``staggered``.

    ``stats``, when given, is filled with the counters (``flagged``, ``uncovered``, ``errored``,
    ``no_base``, ``structures``, ``capped``, ``unbounded``, ``unmeasurable``, ``staggered``,
    ``rolled``, ``no_iv``) so a result can record them next to the figure.

    KNOWN LIMIT: a multi-day trade is measured from the peak at its ENTRY. If the equity sets a
    new peak while the trade is open and the intraday low comes after it, the real dip is
    deeper than this estimate (understated, never overstated). A single contract entered LATER
    into a transaction that already holds other contracts (a roll, a wheel's second leg) is
    still priced alone; it is counted as ``rolled``.
    """
    if drawdown_base is not None and not drawdown_base > 0:
        # A cap is validated positive at config time (equity_cap.validate_equity_cap); a
        # non-positive base here is a wiring bug, and dividing by it would flip or blow up
        # every dip. Refused for the whole call, not skipped trade by trade in the loop below.
        raise ValueError(f"drawdown_base must be > 0 or None, got {drawdown_base!r}")
    refined = max_drawdown
    flagged = 0        # trades/structures that reached the pricing step (already past the dip filter)
    uncovered = 0      # of those, the ones with no usable entry delta / iv / underlying price
    errored = 0        # option trades dropped because a lookup or the arithmetic raised
    no_base = 0        # dips dropped because their denominator (the peak) was not positive
    structures = 0     # flagged MULTI-LEG structures (a subset of ``flagged``)
    no_iv = 0          # of the uncovered structures, those that lacked an iv / rate / price
    no_exit = 0        # structures uncovered because a leg had no exit time
    staggered = 0      # structures priced over the all-legs-held window (legs exit apart)
    capped = 0         # structures whose estimate was bounded by their true max loss
    unbounded = 0      # structures with unbounded loss: no cap, summed-legs estimate only
    unmeasurable = 0   # structures the payoff evaluator refused: no cap (loud)
    rolled = 0         # single contracts entered later into a multi-contract transaction

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

    def _single_leg(t: Dict[str, Any], is_rolled: bool) -> None:
        """One option leg priced on its own -- the pre-structure behaviour, unchanged."""
        nonlocal flagged, uncovered, rolled
        contract = t.get("contract_symbol")
        underlying = t.get("underlying_symbol")
        exit_low = daily_bar_low(underlying, t.get("exit_time"))
        prior_low = prior_daily_bar_low(underlying, t.get("exit_time"))
        if not is_flagged_for_intraday_check(t, prior_low, exit_low):
            return
        flagged += 1
        if is_rolled:
            rolled += 1
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
        """A multi-leg structure priced AS ONE: all legs on the same underlying path, each
        re-priced with Black-Scholes, summed per print, capped at the structure's true maximum
        loss. Never priced from a partial leg set: any leg without an iv, or a run with no iv /
        rate seam, leaves the WHOLE structure uncovered (counted)."""
        nonlocal flagged, uncovered, structures, staggered, capped, no_iv, no_exit
        nonlocal unbounded, unmeasurable
        # Flagged if ANY leg's exit flags it: the legs of one package normally exit together,
        # so they agree; "any" cannot hide a dip one of them shows.
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
        # The window in which ALL legs are held: entry until the FIRST leg exit. Legs that
        # exit apart are no longer the same structure afterwards.
        exits = [t.get("exit_time") for t in rows]
        if any(x is None for x in exits):
            # A leg with no exit time has no window to price: uncovered, with its own counter
            # (``min`` over a None would raise and be filed under the generic ``errored``).
            uncovered += 1
            no_exit += 1
            return
        exit_time = min(exits)
        if len(set(exits)) > 1:
            staggered += 1
        if (len({t["underlying_symbol"] for t in rows}) != 1
                or len({t["contract_symbol"] for t in rows}) != len(rows)):
            uncovered += 1                       # not one package of distinct legs
            return
        if iv_at_entry is None or risk_free_rate is None:
            uncovered += 1
            no_iv += 1
            return
        entry_underlying_px = underlying_price_at(underlying, entry_time)
        ivs = [iv_at_entry(underlying, t["contract_symbol"], entry_time) for t in rows]
        if entry_underlying_px is None or any(v is None for v in ivs):
            uncovered += 1
            no_iv += 1
            return
        legs = []
        for t, iv in zip(rows, ivs):
            expiry = t["expiry"]
            if not hasattr(expiry, "year"):
                expiry = datetime.date.fromisoformat(str(expiry)[:10])
            legs.append({
                "contract": t["contract_symbol"], "entry_premium": t["entry_price"], "iv": iv,
                "size": t["size"], "multiplier": t["multiplier"],
                "direction_sign": 1.0 if t.get("direction") == "buy" else -1.0,
                "strike": t["strike"],
                "right": OptionRight.CALL if str(t.get("option_type")).lower().endswith("call")
                else OptionRight.PUT,
                "expiry": expiry})
        bars = bars_5m_between(underlying, entry_time, exit_time)
        try:
            worst_pnl = estimate_worst_structure_pnl(
                legs, entry_underlying_px, entry_time, bars, commission_per_trade,
                risk_free_rate(entry_time))
        except StructureUncovered as e:
            uncovered += 1
            no_iv += 1
            logger.debug(f"intraday drawdown refinement: structure uncovered: {e}")
            return
        if worst_pnl is None:
            return
        state, amount = structure_max_loss(rows)
        if amount is not None:
            # The estimate is a model; the structure's payoff is exact. It cannot lose
            # more than its worst payoff plus the commissions it paid.
            floor = -(amount + commission_per_trade * len(rows))
            if worst_pnl < floor:
                worst_pnl = floor
                capped += 1
        elif state == "UNBOUNDED":
            unbounded += 1      # no cap exists: the summed-legs estimate stands alone
        else:
            unmeasurable += 1
        worst_loss = min(0.0, worst_pnl)
        if worst_loss == 0.0:
            return
        _lay_dip(entry_time, worst_loss)

    contracts_by_txn: Dict[Any, set] = {}
    for t in trades:
        if t.get("contract_symbol") and t.get("transaction_id") is not None:
            contracts_by_txn.setdefault(t["transaction_id"], set()).add(t["contract_symbol"])
    for group in _structure_groups(trades):
        option_rows = [t for t in group if t.get("contract_symbol") and t.get("underlying_symbol")]
        if not option_rows:
            continue
        # One distinct contract is ONE leg (possibly several fills of it): priced per row
        # exactly as before, which is what keeps every single-leg run bit-identical.
        singles = len({t["contract_symbol"] for t in option_rows}) == 1
        try:
            if singles:
                tid = option_rows[0].get("transaction_id")
                is_rolled = tid is not None and len(contracts_by_txn.get(tid, ())) > 1
                for t in option_rows:
                    try:
                        _single_leg(t, is_rolled)
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
    if stats is not None:
        stats.update({
            "flagged": flagged, "uncovered": uncovered, "errored": errored, "no_base": no_base,
            "structures": structures, "capped": capped, "unbounded": unbounded,
            "unmeasurable": unmeasurable, "staggered": staggered, "rolled": rolled,
            "no_iv": no_iv, "no_exit": no_exit})
    if flagged or errored:
        pct = uncovered / flagged * 100.0 if flagged else 0.0
        # WARNING, not debug, past a third: the refinement moved from the entry day's own
        # snapshot to the last one strictly BEFORE it, so a contract whose first snapshot IS
        # its entry day now has no prior and drops out. That is the correct answer, but it
        # changes what the refined figure covers, and coverage is not visible in the result.
        # Any trade dropped by an exception or a non-positive base is a WARNING too: those
        # are not "no dip", they are trades the figure silently does not cover.
        loud = pct >= 33.0 or errored > 0 or no_base > 0 or unmeasurable > 0
        msg = (f"intraday drawdown refinement: {flagged - uncovered}/{flagged} flagged trade(s) "
               f"had a usable pre-entry delta ({pct:.0f}% uncovered); {errored} trade(s) skipped "
               f"on an exception, {no_base} on a non-positive drawdown base")
        if structures or rolled:
            msg += (f"; {structures} multi-leg structure(s) priced as one ({capped} capped at "
                    f"their max loss, {unbounded} unbounded/uncapped, {unmeasurable} uncapped "
                    f"as unmeasurable, {no_iv} uncovered for want of an iv/rate, {staggered} "
                    f"priced over the all-legs-held window); {rolled} later-entered single "
                    f"contract(s) of a multi-contract transaction priced alone")
        (logger.warning if loud else logger.info)(msg)
    # Only the dips were floored; ``refined`` starts at the input and only ever moves down.
    return refined
