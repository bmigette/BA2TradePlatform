"""Payoff at expiry for an arbitrary option/stock leg set. Pure: no DB, no network, no broker.

WHY THIS EXISTS RATHER THAN A PER-STRUCTURE MAX-LOSS TABLE. The platform already has one
per-structure risk table — ``OptionsAccountInterface.option_reserve_required`` — and it is BROKER
MARGIN, which is not maximum loss and diverges from it in both directions. A cash-secured put
reserves ``strike * 100`` but can only lose ``(strike - credit) * 100``. A jade lizard reserves
the put strike PLUS the call wing, though its loss is bounded by the put side alone. Both remain
correct as margin; neither is max loss.

A second hand-maintained table would be a second thing to keep correct against sixteen builders,
and it drifts easily: the intuitive max loss for a covered call is "basis minus strike minus
credit", which is WRONG — the strike caps the upside, not the downside, and the real answer is
"basis minus credit" (the stock going to zero). Derived from the legs, it cannot be got wrong
structure-by-structure.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ba2_common.core.types import OrderDirection

#: Leg kinds that carry a strike. ``stock`` is the third valid kind and carries none.
_OPTION_KINDS = ("call", "put")
_ALL_KINDS = ("call", "put", "stock")


@dataclass(frozen=True)
class PayoffLeg:
    """One leg of a structure, as the payoff evaluator sees it.

    ``premium`` is ALWAYS POSITIVE — what was paid for a BUY, what was received for a SELL. The
    direction lives in ``side`` alone, so a caller cannot express "a short leg with a negative
    credit" and silently get a sign flip. Every sign in this module derives from ``side``.

    A STOCK LEG uses ``kind="stock"``, ``strike=None``, ``premium=`` the per-share entry price,
    and the DEFAULT ``multiplier=100.0`` — i.e. one stock leg is the 100 shares that back one
    contract. That keeps a covered call's two legs on the same scale with no arithmetic at the
    call site.

    ``ratio`` is legs per ONE structure unit: a 1x2 put ratio spread has a short leg with
    ``ratio=2``.
    """

    kind: str                        # "call" | "put" | "stock"
    side: OrderDirection             # BUY = long, SELL = short
    premium: float                   # per share, always positive
    strike: Optional[float] = None   # required for call/put, None for stock
    ratio: int = 1
    multiplier: float = 100.0


def validate_legs(legs: Sequence[PayoffLeg]) -> Optional[str]:
    """``None`` when every leg can be priced; otherwise a human-readable reason it cannot.

    RETURNED, NOT RAISED. The caller turns this into a recorded refusal on ONE structure. A
    raise here would escape into the middle of a bar's evaluation and take every other
    structure's decision down with it — the same reasoning as
    ``option_economics.collateral_per_contract``.
    """
    if not legs:
        return "structure has no legs"
    for i, leg in enumerate(legs):
        where = f"leg {i} ({leg.kind})"
        if leg.kind not in _ALL_KINDS:
            return f"{where}: unknown leg kind {leg.kind!r}, expected one of {list(_ALL_KINDS)}"
        if leg.side not in (OrderDirection.BUY, OrderDirection.SELL):
            return f"{where}: side {leg.side!r} is neither BUY nor SELL"
        if (leg.premium is None or isinstance(leg.premium, bool)
                or not math.isfinite(leg.premium) or leg.premium < 0):
            return (f"{where}: premium {leg.premium!r} is not a usable price "
                    f"(must be a finite, non-negative number; the sign lives in `side`)")
        if leg.ratio is None or isinstance(leg.ratio, bool) or leg.ratio <= 0:
            return f"{where}: ratio {leg.ratio!r} must be positive"
        if (leg.multiplier is None or not math.isfinite(leg.multiplier)
                or leg.multiplier <= 0):
            return f"{where}: multiplier {leg.multiplier!r} must be positive"
        if leg.kind in _OPTION_KINDS:
            if (leg.strike is None or isinstance(leg.strike, bool)
                    or not math.isfinite(leg.strike) or leg.strike <= 0):
                return f"{where}: strike {leg.strike!r} is not a usable strike"
    return None


def _sign(side: OrderDirection) -> float:
    """+1 for a long leg, -1 for a short one. The ONLY place direction becomes arithmetic."""
    return 1.0 if side == OrderDirection.BUY else -1.0


def payoff_at(legs: Sequence[PayoffLeg], spot: float) -> float:
    """Total P&L in DOLLARS of ONE structure unit if the underlying expires at ``spot``.

    Assumes ``validate_legs(legs) is None`` — call it first. Passing unvalidated legs will
    raise a ``TypeError`` on the bad leg rather than returning a wrong number, which is the
    intended failure mode.
    """
    total = 0.0
    for leg in legs:
        if leg.kind == "call":
            intrinsic = max(spot - leg.strike, 0.0)
        elif leg.kind == "put":
            intrinsic = max(leg.strike - spot, 0.0)
        else:  # stock
            intrinsic = spot
        s = _sign(leg.side)
        total += (s * intrinsic - s * leg.premium) * leg.ratio * leg.multiplier
    return total


#: The three states of a max-loss answer. Strings rather than an Enum because they are compared,
#: logged and asserted on far more often than they are iterated.
MEASURED = "MEASURED"
UNBOUNDED = "UNBOUNDED"
UNMEASURABLE = "UNMEASURABLE"


@dataclass(frozen=True)
class MaxLossResult:
    """A max-loss answer, in three explicitly named states.

    DELIBERATELY NOT AN ``Optional[float]``. This codebase's recurring defect class is "unknown
    reads as zero", and here that would be doubly bad: a max loss of ``0.0`` makes a structure
    look free to open, and an unbounded structure collapsed to ``0.0`` makes the single most
    dangerous position on the board look like the cheapest. Three states, each named, so a
    caller cannot handle one by accident.

    ``amount`` is set iff ``state == MEASURED`` and is POSITIVE dollars of loss.
    ``reason`` is set iff ``state == UNMEASURABLE``.
    """

    state: str
    amount: Optional[float] = None
    reason: Optional[str] = None


def critical_points(legs: Sequence[PayoffLeg]) -> List[float]:
    """The underlying prices at which the payoff slope can change: zero and every strike.

    The payoff is piecewise linear with kinks ONLY at strikes, so the minimum over the bounded
    region ``[0, highest strike]`` is always attained at one of these points. This makes the
    max-loss search EXACT rather than a sample of the curve.
    """
    points = {0.0}
    for leg in legs:
        if leg.kind in _OPTION_KINDS:
            points.add(float(leg.strike))
    return sorted(points)


def upside_slope(legs: Sequence[PayoffLeg]) -> float:
    """d(payoff)/d(spot) ABOVE every strike, in dollars per dollar of underlying.

    Only calls and stock have intrinsic value up there; every put is worthless. A NEGATIVE slope
    means the payoff falls without limit as the underlying rises — the one and only way an
    option structure's loss can be unbounded.

    The downside needs no equivalent test: below every strike, each short put loses at most its
    own strike, so ``payoff_at(legs, 0)`` is always finite. Losses are unbounded above, never
    below.
    """
    slope = 0.0
    for leg in legs:
        if leg.kind in ("call", "stock"):
            slope += _sign(leg.side) * leg.ratio * leg.multiplier
    return slope


def max_loss(legs: Sequence[PayoffLeg]) -> MaxLossResult:
    """The worst-case loss of ONE structure unit at expiry, as POSITIVE dollars.

    See ``MaxLossResult`` for why this is not a float.
    """
    problem = validate_legs(legs)
    if problem is not None:
        return MaxLossResult(UNMEASURABLE, reason=problem)

    if upside_slope(legs) < 0:
        return MaxLossResult(UNBOUNDED)

    worst = min(payoff_at(legs, s) for s in critical_points(legs))

    # A structure that cannot lose at ANY underlying price is an arbitrage. In practice that
    # never means free money — it means a stale, crossed or mis-signed quote. Reporting it as a
    # max loss of 0 would make it the cheapest thing on the board and the triage would take it
    # every time, at whatever size the budget allows.
    if worst >= 0:
        return MaxLossResult(
            UNMEASURABLE,
            reason=(f"structure shows no losing outcome (worst payoff {worst:.2f} at expiry); "
                    f"a risk-free structure is an arbitrage, so this is a stale or crossed "
                    f"quote rather than free money"))

    return MaxLossResult(MEASURED, amount=-worst)
