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


def _numeric(value) -> Optional[float]:
    """``float(value)`` when it can be a real quantity, else ``None``. NEVER RAISES.

    Lifted from ``option_economics._readable_multiplier``, which established this exact rule
    for this exact problem. ``bool`` is excluded because it is an ``int`` subclass and ``True``
    would silently become a 1-share contract — a 100x understatement of max loss, reported as a
    MEASUREMENT. ``str``/``bytes`` are excluded because ``float("100")`` succeeds and a
    stringly-typed price field is a bug to SURFACE, not to parse.

    Non-finite is not numeric here: a ``nan`` ratio passes every ``<= 0`` comparison (``nan <= 0``
    is False), makes every payoff ``nan``, makes ``min()`` return ``nan``, makes ``worst >= 0``
    False, and so reports a MEASURED max loss of ``nan`` — the "unknown reads as a number" defect
    walking in through this module's own front door.
    """
    if value is None or isinstance(value, (bool, str, bytes)):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def validate_legs(legs: Sequence[PayoffLeg]) -> Optional[str]:
    """``None`` when every leg can be priced; otherwise a human-readable reason it cannot.

    RETURNED, NOT RAISED — for ANY input, including a stringly-typed premium. The caller turns
    this into a recorded refusal on ONE structure; a raise here would escape into the middle of
    a bar's evaluation and take every other structure's decision down with it, which is the very
    thing this function exists to prevent. An earlier version delegated to ``math.isfinite``
    directly and so raised ``TypeError`` on a string field: a validator failing in exactly the
    manner it was written to avoid. Every numeric field now goes through ``_numeric``.
    """
    if not legs:
        return "structure has no legs"
    for i, leg in enumerate(legs):
        where = f"leg {i} ({leg.kind})"
        if leg.kind not in _ALL_KINDS:
            return f"{where}: unknown leg kind {leg.kind!r}, expected one of {list(_ALL_KINDS)}"
        if leg.side not in (OrderDirection.BUY, OrderDirection.SELL):
            return f"{where}: side {leg.side!r} is neither BUY nor SELL"
        premium = _numeric(leg.premium)
        if premium is None or premium < 0:
            return (f"{where}: premium {leg.premium!r} is not a usable price "
                    f"(must be a finite, non-negative number; the sign lives in `side`)")
        ratio = _numeric(leg.ratio)
        if ratio is None or ratio <= 0:
            return f"{where}: ratio {leg.ratio!r} must be positive and finite"
        multiplier = _numeric(leg.multiplier)
        if multiplier is None or multiplier <= 0:
            return f"{where}: multiplier {leg.multiplier!r} must be positive and finite"
        if leg.kind in _OPTION_KINDS:
            strike = _numeric(leg.strike)
            if strike is None or strike <= 0:
                return f"{where}: strike {leg.strike!r} is not a usable strike"
            # A PUT CANNOT BE WORTH MORE THAN ITS STRIKE. Its greatest possible value at expiry
            # is `strike`, at an underlying of zero, so a premium above that is a crossed or
            # mis-signed quote rather than an expensive option. Worth catching HERE because the
            # arbitrage guard in `max_loss` only inspects [0, K_max] and never runs at all for
            # an unbounded structure — so for a short-call-bearing shape this is the only quote
            # sanity check there is. There is deliberately NO equivalent bound for a call: its
            # value is unbounded above, so nothing spot-free can be asserted about it.
            if premium > strike:
                return (f"{where}: premium {premium} exceeds strike {strike}; a put can never "
                        f"be worth more than its strike, so this quote is wrong")
    return None


def _sign(side: OrderDirection) -> float:
    """+1 for a long leg, -1 for a short one. The ONLY place direction becomes arithmetic."""
    return 1.0 if side == OrderDirection.BUY else -1.0


def payoff_at(legs: Sequence[PayoffLeg], spot: float) -> float:
    """Total P&L in DOLLARS of ONE structure unit if the underlying expires at ``spot``.

    Assumes ``validate_legs(legs) is None`` — call it first. Passing unvalidated legs raises
    rather than returning a wrong number, which is the intended failure mode.

    The final branch is an explicit ``stock`` test and not a catch-all ``else``. It used to be
    a catch-all, which meant an unrecognised kind was silently priced AS stock: a ``"future"``
    leg returned a plausible-looking 11,900.0 instead of raising, while this docstring promised
    the opposite. A wrong comment is worse than none, so the code was made to match it.
    """
    total = 0.0
    for leg in legs:
        if leg.kind == "call":
            intrinsic = max(spot - leg.strike, 0.0)
        elif leg.kind == "put":
            intrinsic = max(leg.strike - spot, 0.0)
        elif leg.kind == "stock":
            intrinsic = spot
        else:
            raise ValueError(
                f"payoff_at: unknown leg kind {leg.kind!r}; call validate_legs first")
        s = _sign(leg.side)
        total += (s * intrinsic - s * leg.premium) * leg.ratio * leg.multiplier
    return total


#: The three states of a max-loss answer. Strings rather than an Enum because they are compared,
#: logged and asserted on far more often than they are iterated.
MEASURED = "MEASURED"
UNBOUNDED = "UNBOUNDED"
UNMEASURABLE = "UNMEASURABLE"

#: Below this many dollars, a computed loss is floating-point noise around zero rather than a
#: risk budget, and it is treated as UNMEASURABLE.
#:
#: THIS IS NOT DEFENSIVE PADDING; IT CLOSES A LIVE HOLE. A credit vertical whose credit exactly
#: equals its width has a true max loss of zero, which the arbitrage branch below is meant to
#: catch. With ordinary two-decimal premiums the subtraction frequently lands a few ULPs BELOW
#: zero instead: ``max_loss(short 95c @ 0.60, long 95.5c @ 0.10)`` returned
#: ``MEASURED, amount=1.78e-15``. Measured over 3,200 two-decimal credit-equals-width verticals,
#: 480 (15%) leaked through that way.
#:
#: The consequence is not a rounding error. Sizing is ``floor(budget / max_loss_per_contract)``,
#: so 1.78e-15 sizes 562,949,953,421,312,000 contracts on a $1,000 budget — the max-loss budget,
#: the entire reason this module exists, defeated by an arithmetic artefact. The failure is also
#: ASYMMETRIC: landing on +1e-15 is harmless (UNMEASURABLE), landing on -1e-15 is catastrophic.
#: One cent is far below any real structure's per-unit max loss and far above the noise.
MIN_MEASURABLE_LOSS = 0.01

#: Slope magnitudes are sums of ``ratio * multiplier``, so a genuinely negative slope is at
#: least 1.0 in magnitude. Anything closer to zero than this is floating-point dust from mixed
#: multipliers and means a flat payoff, not an unbounded one. Erring here is not free in either
#: direction: a spurious UNBOUNDED does not merely refuse, it SUBSTITUTES a notional-based
#: budget when undefined risk is permitted, so a bounded structure would be sized by the wrong
#: rule rather than blocked.
_SLOPE_EPSILON = 1e-9


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

    # THE ORDER OF THESE TWO GUARDS IS NOT NEGOTIABLE — do not "tidy" it. The arbitrage test
    # below only inspects [0, K_max], and a plain naked short call has a NON-NEGATIVE payoff
    # across that entire region, so running it first would report every ordinary naked short
    # call as UNMEASURABLE(arbitrage). Worse, a 1x2 call ratio spread has a genuine negative
    # trough inside [0, K_max] and would come back MEASURED at a few hundred dollars while
    # actually losing $488,200 at an underlying of 5,000.
    if upside_slope(legs) < -_SLOPE_EPSILON:
        return MaxLossResult(UNBOUNDED)

    worst = min(payoff_at(legs, s) for s in critical_points(legs))

    # A structure that cannot lose at ANY underlying price is an arbitrage. In practice that
    # never means free money — it means a stale, crossed or mis-signed quote. Reporting it as a
    # max loss of 0 would make it the cheapest thing on the board and the triage would take it
    # every time, at whatever size the budget allows.
    #
    # The comparison carries MIN_MEASURABLE_LOSS rather than testing `>= 0`, because the
    # boundary case this branch exists for — a credit exactly equal to the width — lands a few
    # ULPs on the WRONG side of zero for about 15% of two-decimal premium pairs. See the
    # constant for the measurement and for why a sub-cent "max loss" is not a small number but
    # an unbounded contract count.
    if worst >= -MIN_MEASURABLE_LOSS:
        return MaxLossResult(
            UNMEASURABLE,
            reason=(f"structure shows no meaningful losing outcome (worst payoff {worst:.4f} at "
                    f"expiry, within {MIN_MEASURABLE_LOSS} of break-even); a risk-free structure "
                    f"is an arbitrage, so this is a stale or crossed quote rather than free "
                    f"money"))

    return MaxLossResult(MEASURED, amount=-worst)
