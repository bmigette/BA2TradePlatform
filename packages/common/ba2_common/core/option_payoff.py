"""Payoff at expiry for an arbitrary option/stock leg set. Pure: no DB, no network, no broker.

WHY THIS EXISTS RATHER THAN A PER-STRUCTURE MAX-LOSS TABLE. The platform already has one
per-structure risk table — ``OptionsAccountInterface.option_reserve_required`` — and it is BROKER
MARGIN, which is not maximum loss and diverges from it in both directions. A cash-secured put
reserves ``strike * 100`` but can only lose ``(strike - credit) * 100``. A jade lizard reserves
the put strike PLUS the call wing, though its loss is bounded by the put side alone. Both remain
correct as margin; neither is max loss.

A second hand-maintained table would be a second thing to keep correct against seventeen builders,
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
            #                                                          ^^^^^^^^^^^^^^^^^^
            # PUTS ONLY, and the `kind == "put"` below is load-bearing. An earlier version of
            # this check sat under `kind in _OPTION_KINDS`, i.e. it applied to CALLS TOO —
            # directly contradicting the sentence above it. A deep-ITM call is legitimately
            # worth more than its strike (strike 50 with spot 160 marks around 110), so a
            # perfectly ordinary long call was refused, `max_loss` returned UNMEASURABLE, and
            # the message told the reader "a put can never be worth more than its strike" about
            # a call. Harmless while nothing imports this, and a wrong-refusal debugging session
            # the moment Phase 3 makes max_loss the sizing budget.
            if leg.kind == "put" and premium > strike:
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

#: Slope magnitudes are sums of ``ratio * multiplier``, so a genuinely non-zero slope is at
#: least 1.0 in magnitude. Anything closer to zero than this is floating-point dust from mixed
#: multipliers and means a flat payoff, not an unbounded one.
#:
#: THIS CONSTANT NOW GATES TWO FUNCTIONS AND ERRING COSTS SOMETHING DIFFERENT IN EACH — widening
#: it to buy safety in one direction spends it in the other, so neither reading may be dropped
#: from this comment.
#:
#:   * In ``max_loss``, a SPURIOUS UNBOUNDED does not merely refuse: it SUBSTITUTES a
#:     notional-based budget when undefined risk is permitted, so a bounded structure is sized
#:     by the wrong rule rather than blocked.
#:   * In ``max_profit``, the failure runs the OTHER WAY. A genuinely unbounded structure whose
#:     slope reads under the epsilon returns MEASURED with the best value on ``[0, K_max]``,
#:     which for long premium is the near-worthless tail rather than the open-ended upside. That
#:     number then becomes the numerator of ``w_rr = max_profit / max_loss``, so the structure
#:     is not refused — it is RANKED, on an understated score, against peers scored correctly.
#:     A silent mis-ranking is harder to notice than a refusal.
#:
#: The practical risk on the second is low while ``multiplier`` defaults to 100.0 and ratios are
#: small integers; it is stated because nothing in the type system keeps them that way.
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
        # TWO CAUSES, TWO REMEDIES. This branch fires for ANY non-losing worst case, but an
        # earlier version described every one of them as "within 0.01 of break-even ... a stale
        # or crossed quote". A structure whose WORST outcome is +300 is not within a cent of
        # anything; it is a textbook arbitrage, and the thing to check is how the legs were
        # built, not how fresh the quote is. Same defect, and same fix, as the mirrored branch
        # in `max_profit`.
        if worst <= MIN_MEASURABLE_LOSS:
            return MaxLossResult(
                UNMEASURABLE,
                reason=(f"structure's worst outcome is break-even (worst payoff {worst:.4f} at "
                        f"expiry, within {MIN_MEASURABLE_LOSS} of zero): there is no risk "
                        f"budget to size against, and a zero-risk structure at real prices is "
                        f"a stale or crossed quote rather than free money -- re-pull the chain "
                        f"before trusting this price"))
        return MaxLossResult(
            UNMEASURABLE,
            reason=(f"structure PROFITS at every underlying price (worst payoff {worst:.4f} at "
                    f"expiry): a risk-free arbitrage of this size does not survive in a live "
                    f"chain, so the leg set or the premium signs are wrong -- check how these "
                    f"legs were built before re-quoting"))

    return MaxLossResult(MEASURED, amount=-worst)


#: Mirror of MIN_MEASURABLE_LOSS. A structure whose best outcome is under a cent is not a
#: trade with a thin edge; it is a stale or crossed quote. Same magnitude for the same
#: reason -- one cent is far below any real structure's per-unit profit and far above the
#: floating-point dust a credit-equals-width subtraction leaves behind.
MIN_MEASURABLE_PROFIT = 0.01


@dataclass(frozen=True)
class MaxProfitResult:
    """A max-profit answer, in the same three explicitly named states as MaxLossResult.

    A SEPARATE TYPE rather than reusing MaxLossResult, whose docstring promises ``amount``
    is "POSITIVE dollars of LOSS". One dataclass carrying both meanings is how a caller
    ends up sizing a budget off a profit.

    ``amount`` is set iff ``state == MEASURED`` and is POSITIVE dollars of profit.
    ``reason`` is set iff ``state == UNMEASURABLE``.
    """

    state: str
    amount: Optional[float] = None
    reason: Optional[str] = None


def max_profit(legs: Sequence[PayoffLeg]) -> MaxProfitResult:
    """The best-case profit of ONE structure unit at expiry, as POSITIVE dollars.

    The mirror of ``max_loss``: same critical-points scan, ``max`` where that takes ``min``,
    and the SAME NON-NEGOTIABLE GUARD ORDER for the mirrored reason. A long call is
    non-positive across the whole of ``[0, K_max]`` -- it is simply the debit -- so running
    the "cannot profit" test before the slope test would report every ordinary long call as
    UNMEASURABLE, exactly as running the arbitrage test first reports every naked short call
    that way in ``max_loss``.

    THE LOAD-BEARING PREMISE OF THE SCAN, stated because it is a fact about the leg kinds and
    not about this function: once the slope guard has ruled out unbounded upside, the maximum
    over the bounded region ``[0, K_max]`` IS the global maximum, because profit is never
    unbounded BELOW. The underlying cannot go under zero, and at zero every leg's value is
    already capped — a long put is worth at most ``strike * multiplier``, a short stock leg at
    most its entry ``price * multiplier``, and calls are worthless. This is the counterpart of
    the note in ``upside_slope`` that losses run away above and never below, and it is the
    assumption a future leg kind would break: anything whose value grows without limit as the
    underlying FALLS makes this scan silently return a finite answer for an unbounded profit.
    """
    problem = validate_legs(legs)
    if problem is not None:
        return MaxProfitResult(UNMEASURABLE, reason=problem)

    # THE ORDER OF THESE TWO GUARDS IS NOT NEGOTIABLE — the mirror of the identical note in
    # `max_loss`. A POSITIVE upside slope means the payoff rises without limit as the
    # underlying rises, so no scan of [0, K_max] can name a best case. Run the "cannot
    # profit" test first and every ordinary long call comes back UNMEASURABLE, because a
    # long call's payoff really is non-positive across that whole bounded region: it is the
    # debit below the strike and only crosses zero above the break-even, which lies outside
    # the scan. Unbounded profit is the DEFINING property of long premium, not a defect in
    # it, and a selection mode that quietly demoted every long call would answer "does long
    # premium pay?" by construction rather than by measurement.
    if upside_slope(legs) > _SLOPE_EPSILON:
        return MaxProfitResult(UNBOUNDED)

    best = max(payoff_at(legs, s) for s in critical_points(legs))

    # A structure that cannot profit at ANY underlying price is the mirror of the arbitrage
    # the `max_loss` branch catches, and it means the same thing: a stale, crossed or
    # mis-signed quote rather than a real trade. The canonical shape is a debit spread
    # bought for its full width — a 5.00-wide vertical paid 5.00 — whose best outcome is
    # exactly break-even.
    #
    # The comparison carries MIN_MEASURABLE_PROFIT rather than testing `<= 0` for the same
    # reason `max_loss` carries MIN_MEASURABLE_LOSS: with ordinary two-decimal premiums the
    # debit-equals-width subtraction lands a few ULPs on either side of zero, so about half
    # of these would otherwise be reported as MEASURED with a sub-cent profit. That number
    # then feeds `w_rr = max_profit / max_loss`, where a 1e-15 numerator is not a small
    # score but a rounding artefact ranked against real ones.
    if best <= MIN_MEASURABLE_PROFIT:
        # TWO CAUSES, TWO REMEDIES — and the reason must not blame the wrong one. This branch
        # fires for ANY non-profitable best case, and an earlier version told every one of them
        # it was "within 0.01 of break-even ... a stale or crossed quote". For a 5-wide vertical
        # paid 6.00 the best case is -100.00, which is neither, and that message sends an
        # operator hunting a broken quote that does not exist.
        if best >= -MIN_MEASURABLE_PROFIT:
            return MaxProfitResult(
                UNMEASURABLE,
                reason=(f"structure's best outcome is break-even (best payoff {best:.4f} at "
                        f"expiry, within {MIN_MEASURABLE_PROFIT} of zero): there is no profit "
                        f"to measure, and a structure that cannot profit at ANY price is a "
                        f"stale or crossed quote rather than a trade -- re-pull the chain "
                        f"before trusting this price"))
        return MaxProfitResult(
            UNMEASURABLE,
            reason=(f"structure LOSES at every underlying price (best payoff {best:.4f} at "
                    f"expiry): the quote is not necessarily broken -- paying more than the "
                    f"width for a spread is a real, legitimately priced trade -- so this is a "
                    f"structure not to open rather than a quote to re-pull; check the strikes "
                    f"and premiums the builder chose"))

    return MaxProfitResult(MEASURED, amount=best)
