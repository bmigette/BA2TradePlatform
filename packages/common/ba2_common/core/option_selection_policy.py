"""Choosing ONE contract from the candidates that fall inside a rule's box. Pure.

THE DIVISION OF LABOUR. A rule states a BOX — "a put, delta 0.10 to 0.25, one month". This module
chooses inside it. The rule owns the strategy's shape; the policy owns which contract best
expresses it today, and the policy's weights are GENES, so the GA searches the choosing rather
than inheriting somebody's guess at it.

WHY EVERY FEATURE IS NORMALISED WITHIN THE CANDIDATE SET. Option prices, spreads and volumes have
no standard range across symbols: "$2.00 of premium" is rich on a $15 stock and negligible on a
$900 one, so an absolute threshold optimised on one universe is meaningless on another. A
contract's RANK among the peers on its own chain is scale-free, and that is what these features
measure.

THE DEFAULT IS A PROVABLE NO-OP. With only ``w_box_center`` at its pinned 1.0, ``pick`` selects
exactly the contract ``option_selector._pick_by`` selects, tie-breaks included. That is what lets
this ship without moving a single existing backtest — proven in
``tests/test_option_selection_policy_noop.py``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence

from ba2_common.core.option_selector import OptionSelectionConfigError, target_strike
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

#: The score a candidate gets on a feature it cannot answer. Features are MAXIMISED, so 0.0 is
#: the worst possible value: unknown never beats known. Same direction as
#: ``option_selector.passes_liquidity``, which fails closed on a missing liquidity field.
_WORST = 0.0

#: Calendar days per year, matching ``option_economics.DAYS_PER_YEAR``.
_DAYS_PER_YEAR = 365.0

FEATURE_NAMES = ("box_center", "premium", "iv", "rvol", "spread")


@dataclass(frozen=True)
class SelectionPolicy:
    """The weights that decide which contract in the box wins. Each non-pinned weight is a gene.

    ``w_box_center`` IS PINNED AT 1.0 AND IS NOT A GENE. Scaling all five weights by the same
    factor changes no ranking, so leaving it free would hand the GA a degenerate direction to
    wander in — budget spent exploring a difference that is not one.

    ``w_iv`` IS THE ONE SIGNED WEIGHT. Premium richness, relative volume and quote tightness have
    an unambiguous good direction. Implied volatility does not: premium SELLERS want rich vol and
    BUYERS want cheap vol, and which is right for a given strategy is exactly the sort of
    question the search should settle rather than inherit.
    """

    w_box_center: float = 1.0
    w_premium: float = 0.0
    w_iv: float = 0.0
    w_rvol: float = 0.0
    w_spread: float = 0.0

    @property
    def is_default(self) -> bool:
        """True when this policy reproduces the pre-policy selector exactly."""
        return (self.w_box_center == 1.0 and self.w_premium == 0.0 and self.w_iv == 0.0
                and self.w_rvol == 0.0 and self.w_spread == 0.0)


@dataclass(frozen=True)
class PolicyContext:
    """Everything about the request that is not the candidate list.

    ``target`` is the box CENTRE in the strike method's own units — a delta for ``delta``, a
    percentage for ``percent_otm``, unused for ``consensus_target``.

    THE BOX FILTER APPLIES ONLY WHEN ``box_min < box_max``. A degenerate or absent box means
    "aim at ``target``, filter nothing", which is what preserves compatibility with the existing
    single-``strike_param`` rules: filtering a chain down to contracts whose delta is exactly
    0.30 would leave nothing at all.
    """

    strike_method: str                      # "delta" | "percent_otm" | "consensus_target"
    #: REQUIRED, not optional. When it was optional, a caller who omitted it silently deleted
    #: the whole premium feature: ``_premium_richness`` returned None for every candidate, the
    #: column flattened to zeros, and a GA-tuned ``w_premium`` stopped applying with no error --
    #: same chain, same policy, different contract. Everything around this already requires a
    #: clock (``select_single``, ``filter_dte``), so the optionality bought nothing.
    today: date
    target: Optional[float] = None
    box_min: Optional[float] = None
    box_max: Optional[float] = None
    spot: Optional[float] = None
    target_price: Optional[float] = None    # for consensus_target
    option_type: Optional[OptionRight] = None


def _mark(c: OptionContract) -> Optional[float]:
    """The contract's price: mid when both sides quote, else last. None when neither exists."""
    return c.mid if c.mid is not None else c.last


def distance_from_target(c: OptionContract, ctx: PolicyContext) -> Optional[float]:
    """How far this contract is from the box centre, in the strike method's own units.

    None when the contract cannot be measured at all (a delta method against a contract with no
    delta). ``eligible`` excludes those candidates outright rather than scoring them worst — see
    its docstring for why that exactness matters.
    """
    if ctx.strike_method == "delta":
        if c.delta is None or ctx.target is None:
            return None
        return abs(abs(c.delta) - abs(ctx.target))
    ts = target_strike(ctx.strike_method, ctx.target, ctx.spot, ctx.target_price,
                       ctx.option_type)
    if ts is None:
        return None
    return abs(c.strike - ts)


def box_value(c: OptionContract, ctx: PolicyContext) -> Optional[float]:
    """The quantity the box's bounds are expressed in, for this contract.

    ``delta`` -> absolute delta. ``percent_otm`` -> how far out of the money, in percent.
    ``consensus_target`` has no parameter, so it has no box and this is never consulted.
    """
    if ctx.strike_method == "delta":
        return None if c.delta is None else abs(c.delta)
    if ctx.strike_method == "percent_otm":
        if not ctx.spot:
            return None
        # BRANCH CALL-FIRST, exactly as ``option_selector.target_strike`` does. This used to
        # branch PUT-first with a CALL fallback, which is the same thing ONLY when option_type
        # is set. With it None -- and it defaults to None -- the two disagreed in opposite
        # directions: target_strike computed a 5% target of 95.0 (the PUT side) while box_value
        # measured strike 95 as -5.0 and strike 105 as +5.0 (the CALL side), so a box of (3, 7)
        # excluded the very contract the target aimed at and admitted its mirror image.
        if ctx.option_type == OptionRight.CALL:
            return (c.strike / ctx.spot - 1.0) * 100.0
        return (1.0 - c.strike / ctx.spot) * 100.0
    return None


def _premium_richness(c: OptionContract, ctx: PolicyContext) -> Optional[float]:
    """Annualised premium as a fraction of the strike: ``(mark / strike) * 365/dte``.

    A per-contract ratio, so it is comparable between a $15 and a $900 underlying before
    normalisation even begins. None when it cannot be computed — notably a same-day expiry,
    which is not a division opportunity (``365/0`` is infinite and infinity beats every peer).
    """
    mark = _mark(c)
    if mark is None or not c.strike or ctx.today is None:
        return None
    dte = (c.expiry - ctx.today).days
    if dte <= 0:
        return None
    return (mark / c.strike) * (_DAYS_PER_YEAR / dte)


def _normalise(values: Sequence[Optional[float]]) -> List[Optional[float]]:
    """Min-max each value onto [0, 1]. ``None`` stays ``None`` for the caller to fail closed.

    A DEGENERATE RANGE (every present value equal, including the single-candidate case) maps
    everything to 0.0 rather than dividing by zero. That is not a cop-out: a feature that cannot
    distinguish the candidates must not contribute to the ranking, and an equal contribution to
    all of them is exactly no contribution.
    """
    def _unknown(v):
        # NaN AND INFINITY ARE UNKNOWN, NOT DATA. Letting either through poisons the whole
        # column, because `min`/`max` over a list containing NaN are ORDER-DEPENDENT: NaN
        # compares False against everything, so `min([0.25, nan])` is 0.25 while
        # `min([nan, 0.25])` is nan. The range then goes NaN, every normalised value goes NaN,
        # and the pick becomes a function of chain order -- exactly the non-determinism
        # `option_selector._tie` was added to abolish, re-entering through a different door.
        return v is None or not math.isfinite(v)

    present = [v for v in values if not _unknown(v)]
    if not present:
        return [None] * len(values)
    lo, hi = min(present), max(present)
    if hi - lo <= 0:
        return [None if _unknown(v) else 0.0 for v in values]
    return [None if _unknown(v) else (v - lo) / (hi - lo) for v in values]


def _maximise(values: Sequence[Optional[float]]) -> List[float]:
    """Normalise a higher-is-better raw feature, failing closed on missing values."""
    return [_WORST if v is None else v for v in _normalise(values)]


def _minimise(values: Sequence[Optional[float]]) -> List[float]:
    """Normalise a lower-is-better raw feature (distance, spread) and invert it.

    A missing value lands on ``_WORST`` AFTER inversion, not before — otherwise "unknown" would
    invert into the best possible score, which is the fail-OPEN this codebase keeps having to
    remove.
    """
    return [_WORST if v is None else 1.0 - v for v in _normalise(values)]


def feature_matrix(candidates: Sequence[OptionContract], ctx: PolicyContext,
                   only: Optional[Sequence[str]] = None) -> Dict[str, List[float]]:
    """Every feature for every candidate, each normalised to [0, 1] and oriented so that
    HIGHER IS BETTER. Keys are ``FEATURE_NAMES``; each value is a list parallel to
    ``candidates``.

    ``only`` restricts the computation to the named features. ``score_all`` passes the
    non-zero-weighted ones: a disabled gene must not merely contribute zero, it must not be
    computed at all (see ``score_all`` for why `0.0 * nan` made that distinction matter).
    ``None`` means all of them, which is what the tests and any diagnostic caller want.
    """
    wanted = FEATURE_NAMES if only is None else tuple(only)
    builders = {
        "box_center": lambda: _minimise([distance_from_target(c, ctx) for c in candidates]),
        "premium": lambda: _maximise([_premium_richness(c, ctx) for c in candidates]),
        "iv": lambda: _maximise([c.implied_volatility for c in candidates]),
        "rvol": lambda: _maximise([None if c.volume is None else float(c.volume)
                                   for c in candidates]),
        "spread": lambda: _minimise([c.spread_pct for c in candidates]),
    }
    return {name: builders[name]() for name in wanted}


def _validate_box(ctx: PolicyContext) -> None:
    """Raise if the box itself is unusable. Called ONCE per pick, not once per candidate.

    An INVERTED box (``box_min > box_max``) cannot mean anything. Silently ignoring it -- which
    is what a single ``box_min >= box_max`` bail-out did -- hands the GA a search that looks
    constrained and is not, and the run reports results for a box nobody configured. This is the
    stance ``OptionSelectionConfigError`` already takes for liquidity gates: a parameter that can
    never select what it claims to is an error, not a verdict.

    ``box_min == box_max`` is NOT inverted. It is a point target and legitimately filters
    nothing -- narrowing a chain to contracts whose delta is exactly 0.30 would leave nothing at
    all, which is how every existing single-``strike_param`` rule would have stopped trading.
    """
    lo, hi = ctx.box_min, ctx.box_max
    if lo is not None and hi is not None and lo > hi:
        raise OptionSelectionConfigError(
            f"Option selection box is inverted: box_min={lo} > box_max={hi}; no contract can "
            f"fall inside it. Swap the bounds, or set them equal for a point target.")


def _in_box(c: OptionContract, ctx: PolicyContext) -> bool:
    """Is this contract inside the rule's box?

    THE BOUNDS ARE INDEPENDENT. A one-sided box ("delta at least 0.10") is a plausible thing to
    configure and used to be discarded wholesale, because the guard bailed out unless BOTH bounds
    were present. Each bound is now applied on its own.

    ``consensus_target`` HAS NO BOX. Its box would have to be expressed in the units of a strike
    parameter it does not have, so ``box_value`` cannot answer and the fail-closed branch below
    would reject 100% of the chain -- a rule that looks configured and silently stops trading,
    which is the failure ``OptionLiquidityDataUnavailable`` exists to abolish for liquidity
    gates. A comment claiming this "is never consulted" is not enforcement; this is.

    A contract whose box quantity cannot be measured fails CLOSED. "I don't know where this
    contract sits" is not a reason to admit it to a band the rule deliberately narrowed.
    """
    if ctx.strike_method == "consensus_target":
        return True
    lo, hi = ctx.box_min, ctx.box_max
    if lo is None and hi is None:
        return True
    if lo is not None and hi is not None and lo == hi:
        return True                       # point target, not a filter
    v = box_value(c, ctx)
    if v is None:
        return False
    if lo is not None and v < lo:
        return False
    if hi is not None and v > hi:
        return False
    return True


def eligible(candidates: Sequence[OptionContract],
             ctx: PolicyContext) -> List[OptionContract]:
    """The candidates the policy is allowed to choose between.

    Under the ``delta`` method a contract with no delta is EXCLUDED, not scored worst.
    ``option_selector._pick_by`` does exactly that (``usable = [c for c in cands if c.delta is
    not None]``, returning None if none remain), and scoring them worst instead would differ
    from it whenever EVERY candidate lacks a delta: ``_pick_by`` returns None, a worst-score
    policy would return an arbitrary contract with no measurable delta at all.

    A NON-FINITE delta is excluded on the same grounds. ``is not None`` let a NaN delta through,
    and it then WON: the range collapsed to the degenerate branch, the NaN candidate normalised
    to 0.0 and inverted to a perfect 1.0. Unknown beating known is the one thing this module
    promises never to do.

    THE TWO UNAIMABLE CASES ARE HANDLED DIFFERENTLY, AND THAT IS DELIBERATE. Each mirrors what
    ``_pick_by`` does with the same input, because the no-op guarantee is worth more than
    consistency between them:

      * ``delta`` with no target -- ``_pick_by`` RAISES (``abs(None)``). Raising
        ``OptionSelectionConfigError`` is the same outcome through the front door:
        ``_OptionEntryAction.execute`` already catches it and reports the exact knob.
      * a non-delta method whose ``target_strike`` is None -- ``_pick_by`` RETURNS None, and so
        must this. The live case is ``consensus_target`` on a recommendation carrying no target
        price, which ``select_single`` reaches with its default ``target_price=None``. Turning
        that into a raise would be a live behaviour change dressed up as a fix; if the silence
        is wrong, it is wrong in both and belongs in a later phase that changes both together.
    """
    _validate_box(ctx)
    out = list(candidates)
    if ctx.strike_method == "delta":
        if ctx.target is None:
            raise OptionSelectionConfigError(
                "Option selection uses the 'delta' strike method but no target delta was "
                "given, so no contract can be aimed at. Set the strike parameter.")
        out = [c for c in out if c.delta is not None and math.isfinite(c.delta)]
    elif target_strike(ctx.strike_method, ctx.target, ctx.spot, ctx.target_price,
                       ctx.option_type) is None:
        return []
    return [c for c in out if _in_box(c, ctx)]


def score_all(candidates: Sequence[OptionContract], ctx: PolicyContext,
              policy: SelectionPolicy) -> List[float]:
    """The weighted score of each candidate. Higher wins."""
    weights = {"box_center": policy.w_box_center, "premium": policy.w_premium,
               "iv": policy.w_iv, "rvol": policy.w_rvol, "spread": policy.w_spread}
    # SKIP ZERO WEIGHTS ENTIRELY -- do not compute the feature and do not multiply by it.
    #
    # `0.0 * x` looks like it removes a disabled gene from the decision. It does not: `0.0 * nan`
    # is `nan`, so ONE non-finite value in ANY of the five source fields turned every score into
    # NaN, `min()` compared NaN tuples (every comparison False) and returned candidate #0 BY LIST
    # ORDER. Four of those fields -- iv, volume, bid, ask -- are ones the selector this must
    # imitate never reads at all, so the no-op guarantee was silently conditional on data hygiene
    # in columns nobody was checking.
    #
    # Not computing the feature is also what makes the default policy affordable. Measured on a
    # 200-contract chain, 2000 iterations: the legacy selector is 27.1 us/call, this with all
    # five genes active is 388.8 us, and this at default weights is 99.5 us -- so the skip is
    # worth 3.9x. The residual 3.7x over legacy is the cost of the mechanism itself (building
    # and normalising the box_center column) and is the number to attack if the GA hot path
    # ever needs it; it is per structure, per bar, per symbol.
    active = [name for name in FEATURE_NAMES if weights[name]]
    m = feature_matrix(candidates, ctx, only=active)
    return [sum(weights[name] * m[name][i] for name in active)
            for i in range(len(candidates))]


def pick(candidates: Sequence[OptionContract], ctx: PolicyContext,
         policy: SelectionPolicy) -> Optional[OptionContract]:
    """The single best contract in the box, or None when the box is empty.

    THE TIE-BREAK IS THE EXISTING ONE. Ties resolve to the LOWEST STRIKE and then the EARLIEST
    EXPIRY, matching ``option_selector._tie``. That ordering is not cosmetic: the historical
    cache lists the same strike under more than one in-window expiry, so candidates routinely
    tie on the distance metric, and before the expiry term existed ``min()`` resolved them by
    input-list order — reversing the chain changed which contract every structure pinned itself
    to.

    Implemented as ``min`` over ``(-score, strike, expiry)`` rather than ``max`` over score,
    because that makes the two tie-break terms read in their natural ascending direction and
    keeps them identical to the legacy key.
    """
    cands = eligible(candidates, ctx)
    if not cands:
        return None
    scores = score_all(cands, ctx, policy)
    best = min(range(len(cands)),
               key=lambda i: (-scores[i], cands[i].strike, cands[i].expiry))
    return cands[best]
