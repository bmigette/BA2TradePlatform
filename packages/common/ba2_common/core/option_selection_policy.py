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
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from ba2_common.core.option_payoff import (
    MEASURED,
    MIN_MEASURABLE_LOSS,
    UNBOUNDED,
    MaxLossResult,
    PayoffLeg,
    max_loss,
    max_profit,
)
from ba2_common.core.option_request import (
    BUDGET_CEILING_REFUSAL,
    BUDGET_EXHAUSTED_REFUSAL,
    EMPTY_BOX_REFUSAL,
    EMPTY_CHAIN_REFUSAL,
    MAX_LOSS_UNMEASURABLE_REFUSAL,
    SELECTION_CONFIG_REFUSAL,
    UNDEFINED_RISK_REFUSAL,
    validate_refusal_phrase,
)
from ba2_common.core.option_selector import OptionSelectionConfigError, target_strike
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight, OrderDirection

#: The score a candidate gets on a feature it cannot answer. Features are MAXIMISED, so 0.0 is
#: the worst possible value: unknown never beats known. Same direction as
#: ``option_selector.passes_liquidity``, which fails closed on a missing liquidity field.
_WORST = 0.0

#: Calendar days per year, matching ``option_economics.DAYS_PER_YEAR``.
_DAYS_PER_YEAR = 365.0

FEATURE_NAMES = ("box_center", "premium", "iv", "rvol", "spread", "profit", "rr")

#: The features whose value is a property of the WHOLE STRUCTURE rather than of one contract, so
#: they exist only when ``PolicyContext.structure_fn`` can complete a candidate into its legs.
#: They are the only features that can be INAPPLICABLE -- see ``inapplicable_features``.
PAYOFF_FEATURES = ("profit", "rr")


class _OffScale:
    """A measurement that EXISTS but has no number: an UNBOUNDED payoff.

    NOT THE SAME THING AS ``None``, AND THE DIFFERENCE IS THE WHOLE DESIGN. ``None`` means the
    measurement is ABSENT -- a crossed quote, a leg that could not be priced -- and absent fails
    closed, scoring ``_WORST``, because a contract whose peers report a value and which cannot
    report its own deserves to lose to them. UNBOUNDED is the opposite situation: nothing is
    missing, the shape is understood perfectly, and its value is simply off the end of the
    scale.

    THE TWO SHARED A REPRESENTATION ONCE AND IT COST A RULE. When UNBOUNDED collapsed to
    ``None``, a long call sitting among credit verticals was the only candidate without a number
    and so scored ``_WORST`` alone while its peers scored real values -- ``w_profit`` silently
    deleting long premium from any mixed chain, with ``inapplicable_features`` reporting nothing
    amiss because the COLUMN was not empty. Measured: profit column ``[0.0, 1.0, 0.0]`` and the
    pick moved off the long call. Unbounded profit is the DEFINING property of a long call, and
    the grid exists partly to measure whether long premium pays; demoting it answers that
    question by construction.

    Compared by IDENTITY (``is``), never by equality, so it can never be mistaken for a price.
    """

    __slots__ = ()

    def __repr__(self) -> str:            # pragma: no cover - a debugging aid only
        return "OFF_SCALE"


#: The single instance. See ``_OffScale``.
_OFF_SCALE = _OffScale()

#: One entry of a raw payoff column: a number, ``None`` for an absent measurement, or
#: ``_OFF_SCALE`` for one that is off the end of the scale.
_PayoffValue = Union[float, None, _OffScale]


@dataclass(frozen=True)
class SelectionPolicy:
    """The weights that decide which contract in the box wins. Each non-pinned weight is a gene.

    ``w_box_center`` IS PINNED AT 1.0 AND IS NOT A GENE. Scaling EVERY weight by the same factor
    changes no ranking, so leaving it free would hand the GA a degenerate direction to wander in
    — budget spent exploring a difference that is not one.

    ``w_iv`` IS THE ONE SIGNED WEIGHT. Premium richness, relative volume and quote tightness have
    an unambiguous good direction. Implied volatility does not: premium SELLERS want rich vol and
    BUYERS want cheap vol, and which is right for a given strategy is exactly the sort of
    question the search should settle rather than inherit.

    ``w_profit`` AND ``w_rr`` CAN BE INERT RATHER THAN ZERO, which no other weight can be. They
    read the payoff of the whole structure, so a builder that has not been taught
    ``PolicyContext.structure_fn`` — or a shape with unbounded PROFIT, which is every long call
    — leaves them nothing to rank. ``inapplicable_features`` reports exactly when that is
    happening, so the condition is visible instead of being a gene the GA can never move. An
    unbounded LOSS is not one of those cases: ``rr`` prices it synthetically and scores it low,
    because "low" is the honest answer for a naked short and "unknown" is not.
    """

    w_box_center: float = 1.0
    w_premium: float = 0.0
    w_iv: float = 0.0
    w_rvol: float = 0.0
    w_spread: float = 0.0
    w_profit: float = 0.0
    w_rr: float = 0.0

    @property
    def is_default(self) -> bool:
        """True when this policy reproduces the pre-policy selector exactly.

        EVERY WEIGHT MUST BE LISTED HERE. This property is what the no-op guarantee is asserted
        through, so a weight it forgets to look at is a weight that can change a pick while the
        policy still reports itself as changing nothing.
        """
        return (self.w_box_center == 1.0 and self.w_premium == 0.0 and self.w_iv == 0.0
                and self.w_rvol == 0.0 and self.w_spread == 0.0
                and self.w_profit == 0.0 and self.w_rr == 0.0)


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
    #: Turns a candidate contract into the FULL leg list of the structure it would become.
    #: Supplied by the builder, which is the only thing that knows its own shape -- the policy
    #: must not learn structure shapes and the builder must not learn scoring.
    #:
    #: THE SEAM EXISTS BECAUSE max_profit/max_loss ARE PROPERTIES OF A STRUCTURE, NOT OF A
    #: CONTRACT. For a single-leg shape (long call, cash-secured put) the candidate IS the
    #: structure, but for a vertical the policy picks one leg and the builder derives the wing
    #: afterwards with ``select_wing`` -- so at the moment ``pick`` runs there is nothing to
    #: measure unless the builder hands over a way to complete the shape.
    #:
    #: None means the profit/rr features are INAPPLICABLE for this pick, not that they score
    #: zero. See ``inapplicable_features``. That is what lets these features ship before all 17
    #: builders supply a closure: an untaught builder loses the feature VISIBLY.
    #:
    #: THE CLOSURE DECLINES BY RETURNING None OR AN EMPTY LIST — no wing left on the chain, say.
    #: That is one missing value, scored like any other missing value, and not an error.
    #:
    #: AN EXCEPTION IS A DIFFERENT THING AND IT PROPAGATES, taking the whole pick down. That is
    #: deliberate: a closure that RAISES has a defect in the builder (it read a field that is
    #: not there, or computed a wing from a None strike), and catching it here would turn a
    #: broken builder into quietly worse selection on every bar, with nothing in the run naming
    #: the builder or the line. Refusals belong to the layer that can report them —
    #: ``_OptionEntryAction.execute`` already catches and names the knob — not to a silent
    #: except in the scoring loop.
    structure_fn: Optional[Callable[[OptionContract], Optional[Sequence[PayoffLeg]]]] = None
    #: Dollars of max loss ONE contract may risk. None disables the filter entirely and is a
    #: provable no-op -- ``eligible`` returns before the builder is asked anything.
    #:
    #: THIS IS ``min(instrument_left, structure_cap)``, NOT THE FULL BUDGET. ``book_left`` depends
    #: on which structures a bar's greedy triage admits first, so it does not exist yet at
    #: selection time; threading a number that does not exist would be a fiction.
    #:
    #: NONE OF THOSE THREE QUANTITIES EXISTS IN THIS REPOSITORY YET -- ``book_left``,
    #: ``instrument_left`` and ``structure_cap`` are design vocabulary with zero hits in the
    #: code, and the triage that would compute them is unwritten. This field is therefore
    #: PLUMBING AHEAD OF ITS CALLER, and the name records what the caller must eventually pass
    #: rather than describing something that flows today. ``_resolve`` will supply it.
    #:
    #: WHAT IT WILL BUY. A max-loss budget refuses the ONE contract the picker handed it and
    #: never goes back for a cheaper strike that would have fitted; the ceiling puts the budget
    #: upstream of the choice so a cheaper strike can still be taken. Stated in the future tense
    #: on purpose: see ``eligible`` for what does and does not enforce a budget today.
    max_loss_ceiling: Optional[float] = None
    #: Whether an UNBOUNDED-loss structure may be charged a synthetic figure and admitted at all.
    #: False (the default) means undefined risk is NOT permitted here, which is the design's
    #: default refusal.
    #:
    #: THIS EXISTS SO THE CEILING CANNOT SILENTLY OVERRIDE THE ACCOUNT'S SETTING. Collapsing
    #: UNBOUNDED into "excluded" would make a PERMITTED naked short unselectable while
    #: ``allow_undefined_risk_options`` still read as ON; collapsing it into "charged" would sell
    #: undefined risk on an account that forbids it. The two refusals are different in KIND -- one
    #: is a want of PERMISSION, the other a want of a NUMBER -- and ``_chargeable_max_loss`` keeps
    #: them in separate branches for that reason.
    allow_undefined_risk: bool = False


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


def _unbounded_risk_leg(legs: Sequence[PayoffLeg]) -> Optional[PayoffLeg]:
    """The leg that CARRIES a structure's unbounded loss, or None when no single one does.

    ``max_loss`` returns UNBOUNDED for exactly one reason: ``upside_slope`` is negative, i.e. the
    structure is net short calls or short stock and the payoff falls without limit as the
    underlying rises. So the leg to attribute the risk to is the short call/stock leg — for a
    naked short call, its own strike.

    DELIBERATELY NOT A GENERAL ATTRIBUTION ENGINE. Exactly one short upside leg with a usable
    strike, or nothing. Two short calls at different strikes have no single assignment cost, and
    a short STOCK leg has no strike at all; in both cases an invented denominator is worse than
    an absent feature, because a made-up number competes on the same scale as measured ones and
    nothing downstream can tell them apart afterwards.
    """
    exposed = [leg for leg in legs
               if leg.side == OrderDirection.SELL and leg.kind in ("call", "stock")]
    if len(exposed) != 1:
        return None
    leg = exposed[0]
    if leg.kind != "call" or not leg.strike or leg.strike <= 0:
        return None
    return leg


def _risk_denominator(legs: Sequence[PayoffLeg], loss: MaxLossResult) -> Optional[float]:
    """The dollars of risk that ``rr`` divides by. NOT always the max loss — read on.

    THREE ANSWERS, ONE PER STATE:

      * MEASURED — the measured amount, and nothing clever.
      * UNBOUNDED — a SYNTHETIC denominator: the assignment cost of the leg carrying the
        unbounded risk, ``strike * multiplier * ratio``. A naked short's true reward-to-risk is
        ``profit / infinity -> 0``, so LOW is the honest answer rather than UNKNOWN, and
        refusing to rank these would hide the exact comparison the grid exists to make — how
        undefined-risk premium selling scores against defined-risk premium selling. This reuses
        the substitution the design already makes when undefined risk is permitted rather than
        inventing a second convention for the same problem.
      * UNMEASURABLE — None. A broken quote is NEVER priced by rule. The short call in an
        arbitrage vertical has a perfectly usable strike, so the synthetic figure is right there
        for the taking; taking it would launder a crossed or stale quote into a number that then
        competes on the same scale as real ones.

    THE DENOMINATOR IS ``strike``-BASED AND NOT ``spot``-BASED, AND THAT IS LOAD-BEARING. The
    budgeting rule this borrows from is worded in terms of spot, but ``spot`` does not vary
    between candidates within a single pick — so a spot-based denominator would make ``rr`` a
    pure rescale of ``profit``, min-max normalisation would emit two IDENTICAL columns, and
    ``w_rr`` would be perfectly collinear with ``w_profit``: two genes searching one dimension,
    which is the dead-gene failure this module keeps legislating against. ``strike`` varies per
    candidate, so ``rr`` stays genuinely distinct AND still means something real — credit per
    dollar of assignment cost.

    ``multiplier * ratio`` rather than a hardcoded ``* 100``: both default to the 100-share
    contract, so every structure the platform builds today is unaffected, but a 1x2 ratio
    spread's assignment cost really is doubled and hardcoding 100 would understate the risk of
    precisely the shape whose risk is worst.
    """
    if loss.state == MEASURED:
        return loss.amount
    if loss.state != UNBOUNDED:
        return None
    leg = _unbounded_risk_leg(legs)
    if leg is None:
        return None
    return float(leg.strike) * float(leg.multiplier) * float(leg.ratio)


def _profit_and_risk(c: OptionContract,
                     ctx: PolicyContext) -> Tuple[_PayoffValue, Optional[float]]:
    """``(max_profit, risk)`` in dollars for the structure this candidate would become.

    THE SECOND ELEMENT IS NOT THE MAX LOSS and must never be used as a sizing budget — that is
    why this is not called ``_payoff_pair``. It is the ``rr`` DENOMINATOR, which for an unbounded
    structure is a synthetic assignment cost standing in for a loss that has no number. Sizing
    against it would treat a naked short call as risking its strike, which is the understatement
    ``option_payoff`` exists to refuse. Callers needing the real thing call ``max_loss``.

    EACH OF THE THREE PAYOFF STATES GETS ITS OWN ANSWER ON THE PROFIT SIDE, and collapsing any
    two of them loses a rule:

      * MEASURED     -> the amount.
      * UNBOUNDED    -> ``_OFF_SCALE``. Not missing. Read that class's docstring before changing
                        this line; it records what collapsing it into ``None`` cost.
      * UNMEASURABLE -> ``None``. Genuinely absent, fails closed, scores ``_WORST``.

    The risk side has no ``_OFF_SCALE`` because ``_risk_denominator`` already substitutes a
    number for an unbounded loss, which is a stand-in the profit side has no honest equivalent
    of: there is no finite figure that means "the upside is open-ended".
    """
    if ctx.structure_fn is None:
        return None, None
    # NOTE: the closure is called WITHOUT a try/except, on purpose. Declining a candidate is
    # None or an empty list; an exception is a defect in the builder and propagates. See
    # ``PolicyContext.structure_fn``.
    legs = ctx.structure_fn(c)
    if not legs:
        # The builder declined this candidate — it could not complete the shape around it. A
        # missing VALUE, not an error: the remaining candidates still rank against each other.
        return None, None
    profit = max_profit(legs)
    if profit.state == MEASURED:
        profit_value: _PayoffValue = profit.amount
    elif profit.state == UNBOUNDED:
        profit_value = _OFF_SCALE
    else:
        profit_value = None
    return profit_value, _risk_denominator(legs, max_loss(legs))


def _reward_to_risk(profit: _PayoffValue, risk: Optional[float]) -> _PayoffValue:
    """``max_profit / risk`` for one candidate.

    TAKES THE PAIR RATHER THAN THE CONTRACT so that both features can be served from a single
    payoff pass — see ``payoff_columns`` for why that matters.

    THE NUMERATOR'S STATE CARRIES THROUGH. An off-scale profit over a finite risk is an
    off-scale ratio, so it stays ``_OFF_SCALE`` and takes the whole column inert with it rather
    than being flattened into either a missing value or a very large one. Substituting a big
    float would rank a long call as the most attractive thing on the chain precisely BECAUSE its
    upside cannot be measured — the "unknown beats known" inversion this module exists to
    refuse; substituting ``None`` would demote it, which is the same refusal wearing the
    costume of fail-closed.

    THE MISSING DENOMINATOR WINS OVER THE OFF-SCALE NUMERATOR, and the order of the guards below
    says so. If the risk could not be established at all — an UNMEASURABLE max loss, i.e. a
    broken quote — then the ratio is ABSENT rather than off-scale, and absent fails closed. A
    broken quote must not be able to take a whole column inert; that would let one crossed
    market disable a gene for every candidate beside it.

    THE CEILING FILTER TAKES THE OPPOSITE VIEW OF AN UNMEASURABLE LOSS, ON PURPOSE. Where this
    function demotes such a candidate and keeps it in the set, ``_chargeable_max_loss`` charges it
    infinity and removes it outright. Neither is the other's bug: a ranking can absorb an unknown
    by scoring it worst, whereas a budget cannot spend one. Recorded in both places so that a
    later "unify these two" refactor has to argue with the reason rather than discover it.

    THAT IS A TRADE, AND THIS IS WHAT IT COSTS. A long call bought for 0.00 — an ordinary 0-bid
    far strike, not a contrived input — is UNBOUNDED on profit and UNMEASURABLE on loss at the
    same time, and this precedence DEMOTES it: it scores ``_WORST`` on ``rr`` while its bounded
    peers score real ratios. That is the outcome the governing constraint forbids, arriving
    through the candidate's own broken quote rather than through its unbounded profit, and the
    effect on the pick is the same either way. It is accepted because the alternative fails in
    KIND rather than in degree: one stale quote anywhere in the chain would disable the gene for
    every candidate beside it, silently converting a data outage into a dead gene, whereas this
    confines the damage to the candidate whose quote is actually broken. ``profit``, which reads
    the UNBOUNDED state instead of the loss, still refuses to demote it.

    The ``risk <= 0`` guard is belt and braces. ``max_loss`` refuses to report an amount at or
    below ``MIN_MEASURABLE_LOSS`` and a synthetic denominator is a positive strike times a
    positive multiplier, so this cannot fire today; it is here because dividing by a sub-cent
    denominator is how a floating-point artefact turns into an astronomical score that wins
    every pick, and that failure mode has already been paid for once on the sizing path (see
    that constant's comment).
    """
    if risk is None or risk <= 0:
        return None
    if profit is _OFF_SCALE:
        return _OFF_SCALE
    if profit is None:
        return None
    return profit / risk


#: What ``payoff_columns`` returns: the raw ``profit`` column and the raw ``rr`` column, each
#: parallel to the candidate list. Named because it is now passed BETWEEN the two public entry
#: points rather than being an internal detail of each.
PayoffColumns = Tuple[List[_PayoffValue], List[_PayoffValue]]


def payoff_columns(candidates: Sequence[OptionContract], ctx: PolicyContext) -> PayoffColumns:
    """The raw ``profit`` and ``rr`` columns, built in ONE pass over the candidates.

    ONE PASS IS THE POINT. Both features read the same two payoff evaluations, so computing them
    feature-by-feature would run ``structure_fn`` plus ``max_profit`` plus ``max_loss`` TWICE per
    candidate whenever both genes are live — on a path that runs per structure, per bar, per
    symbol. A memo keyed on the contract is not available (``OptionContract`` is a mutable
    dataclass and therefore unhashable, so it cannot be a dict key by value); computing both
    columns together sidesteps the question entirely.

    PUBLIC SO THAT ONE CALLER CAN FEED BOTH ``feature_matrix`` AND ``inapplicable_features``,
    which is worth more than the saved microseconds. The two share ``_column_cannot_rank``, so
    they cannot drift in their PREDICATE — but computing separately they would each invoke
    ``structure_fn`` afresh, so a stateful or non-deterministic closure could still make the
    report describe different numbers from the ones the ranking used. Passing one result to both
    closes that hole as well as halving the work. Measured on a 200-row chain: 5039us for the
    pass, so doing it twice is not a rounding error either.
    """
    pairs = [_profit_and_risk(c, ctx) for c in candidates]
    return ([profit for profit, _ in pairs],
            [_reward_to_risk(profit, risk) for profit, risk in pairs])


def _column_cannot_rank(column: Sequence[_PayoffValue]) -> bool:
    """Is this payoff column unable to rank the candidate set AT ALL?

    TWO WAYS TO GET THERE, and they are not the same shape of problem:

      * NOTHING IS PRESENT — every candidate is ``None``. An untaught builder (no
        ``structure_fn``), or a chain where nothing could be priced.
      * SOMETHING IS OFF THE SCALE — ANY candidate is ``_OFF_SCALE``. You cannot min-max
        infinity against 300 on a normalised scale, and there is no honest place to put the
        unbounded one: worst DEMOTES it (and demoting long calls is the one thing this design
        forbids), best OVER-promotes it so the weight would always pick long premium in a mixed
        set. Neither is a measurement, so the column declines to rank.

    ONE ``_OFF_SCALE`` IS ENOUGH, WHERE ONE ``None`` IS NOT. That asymmetry is the entire point.
    A single absent value is a defect in THAT candidate and it should lose to peers that can
    report; a single off-scale value is a shape whose peers simply cannot be compared with it.
    """
    return any(v is _OFF_SCALE for v in column) or all(v is None for v in column)


def _maximise_payoff(column: Sequence[_PayoffValue]) -> List[float]:
    """``_maximise`` for a payoff column, plus the inert rule from ``_column_cannot_rank``.

    Returning ``_WORST`` for EVERY candidate is what "inert" means mechanically: the weight
    multiplies a constant, every score shifts by the same amount, and the ranking is untouched.
    It is deliberately the same value ``_maximise`` already produces for an all-``None`` column,
    so the two inert paths cannot drift apart.
    """
    if _column_cannot_rank(column):
        return [_WORST] * len(column)
    # Past the guard nothing is ``_OFF_SCALE``, so what remains is the ordinary
    # ``Optional[float]`` column ``_maximise`` has always taken.
    return _maximise(column)


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
                   only: Optional[Sequence[str]] = None,
                   payoff: Optional[PayoffColumns] = None) -> Dict[str, List[float]]:
    """Every feature for every candidate, each normalised to [0, 1] and oriented so that
    HIGHER IS BETTER. Keys are ``FEATURE_NAMES``; each value is a list parallel to
    ``candidates``.

    ``only`` restricts the computation to the named features. ``score_all`` passes the
    non-zero-weighted ones: a disabled gene must not merely contribute zero, it must not be
    computed at all (see ``score_all`` for why `0.0 * nan` made that distinction matter).
    ``None`` means all of them, which is what the tests and any diagnostic caller want.

    AN UNRANKABLE COLUMN COMES OUT UNIFORM, NOT WORST-FOR-SOMEONE. Every candidate scores the
    same and the weight cannot move the ranking at all. That is the intended INERT behaviour
    rather than an accident of the fail-closed rule, and it is the difference between "this
    feature has nothing to say here" and "this structure is bad": an UNBOUNDED max profit is the
    defining property of a long call, and demoting it for that would answer "does long premium
    pay?" by construction instead of by measurement. ``_column_cannot_rank`` holds the two ways
    a payoff column gets there; ``inapplicable_features`` reports which ones did.

    ``payoff`` accepts an already-computed ``payoff_columns`` result. Pass the SAME object to
    ``inapplicable_features`` when you want both: it halves the work and, more importantly,
    guarantees the report describes the very numbers this ranking used.
    """
    wanted = FEATURE_NAMES if only is None else tuple(only)

    # LAZY, MEMOISED, AND SHARED BY THE TWO PAYOFF FEATURES. Lazy because a caller who wants
    # neither -- which is every default-policy pick, since ``score_all`` passes only the
    # non-zero-weighted features -- must not call ``structure_fn`` at all. Memoised because when
    # both genes ARE live the two builders below would otherwise repeat the whole payoff pass.
    # A one-element list is the cell; ``nonlocal`` would need a def, and this stays local to the
    # call so nothing about ``PolicyContext`` has to become mutable or hashable.
    #
    # THE ANNOTATION SAYS ``_PayoffValue`` AND MUST KEEP SAYING IT. This is the spot a reader
    # checks to answer "can an off-scale value reach the normaliser?", so an ``Optional[float]``
    # here -- which is what it said once -- answers no while the value says yes.
    memo: List[Optional[PayoffColumns]] = [payoff]

    def _columns() -> PayoffColumns:
        if memo[0] is None:
            memo[0] = payoff_columns(candidates, ctx)
        return memo[0]

    builders = {
        "box_center": lambda: _minimise([distance_from_target(c, ctx) for c in candidates]),
        "premium": lambda: _maximise([_premium_richness(c, ctx) for c in candidates]),
        "iv": lambda: _maximise([c.implied_volatility for c in candidates]),
        "rvol": lambda: _maximise([None if c.volume is None else float(c.volume)
                                   for c in candidates]),
        "spread": lambda: _minimise([c.spread_pct for c in candidates]),
        "profit": lambda: _maximise_payoff(_columns()[0]),
        "rr": lambda: _maximise_payoff(_columns()[1]),
    }
    return {name: builders[name]() for name in wanted}


def inapplicable_features(candidates: Sequence[OptionContract], ctx: PolicyContext,
                          payoff: Optional[PayoffColumns] = None) -> Tuple[str, ...]:
    """The features that cannot rank THIS candidate set at all, so their weights are inert.

    DISTINCT FROM A MISSING VALUE ON ONE CANDIDATE, which fails closed and scores ``_WORST``
    exactly as it always has. A feature lands here when no ``structure_fn`` was supplied, when
    no candidate could be priced at all, or when ANY candidate's value is off the scale — see
    ``_column_cannot_rank`` for why one unbounded candidate is enough where one absent one is
    not.

    AN UNBOUNDED LOSS NO LONGER LANDS HERE. ``rr`` prices it with a synthetic denominator (see
    ``_risk_denominator``), so a naked short call ranks LOW rather than refusing to rank; ``rr``
    only goes inert on the risk side when no single strike carries the unbounded risk, e.g. a
    short stock leg. That asymmetry is deliberate: one unpriceable quote
    among good ones is a defect in that candidate and it should lose to its peers, whereas a
    column with nothing in it is a defect in the QUESTION and must not sort anybody.

    REPORTED RATHER THAN RAISED, and rather than silently contributing zero. Raising would crash
    a perfectly valid long-call arm over a weight it should simply ignore; silence would leave
    the GA burning budget on a gene that can never move a pick, with nothing in the run saying
    so. Mirrors ``option_book.RailVerdict.evaluated``, which records that
    ``undefined_risk_max_pct`` is genuinely dead for a debit arm instead of pretending it passed.

    ONLY ``PAYOFF_FEATURES`` CAN APPEAR HERE. The other five read a field off the contract in
    front of them, so they are always applicable in principle — a chain where every candidate
    lacks an IV is a data outage, not a shape that has no IV, and conflating the two would let a
    silent feed failure quietly disable a gene mid-run.

    An empty candidate list reports both features inapplicable, which is vacuously true and
    costs nothing: ``pick`` has already returned None before any weight is consulted.

    ``payoff`` accepts an already-computed ``payoff_columns`` result, and a caller that also
    wants a ``feature_matrix`` should pass one object to both. Without it this function makes a
    SECOND full payoff pass (5039us on a 200-row chain, matching the matrix's own), and the two
    passes call ``structure_fn`` independently — so a stateful closure could make this report
    describe numbers the ranking never saw. Sharing ``_column_cannot_rank`` prevents the two
    from drifting in their predicate; only sharing the columns prevents them drifting in their
    input.
    """
    profit_column, rr_column = (payoff_columns(candidates, ctx) if payoff is None else payoff)
    columns = {"profit": profit_column, "rr": rr_column}
    return tuple(name for name in PAYOFF_FEATURES if _column_cannot_rank(columns[name]))


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


#: WHY a charge came back infinite, or didn't. ``_ceiling_reason`` reads this to tell three
#: fixable causes apart -- ``math.inf`` alone cannot, and F12 is exactly the bug that resulted:
#: a permission refusal, a genuinely unpriceable candidate, and a real-but-too-rich charge all
#: collapsed into one phrase that sent the operator to re-pull quotes when the fix was a SETTING.
#:
#:   * ``_CAUSE_PRICED``      -- a real dollar figure exists. MEASURED, or UNBOUNDED with
#:                               ``allow_undefined_risk`` and a leg to price the assignment
#:                               against. The remedy, if it was refused, is a bigger ceiling.
#:   * ``_CAUSE_PERMISSION``  -- UNBOUNDED, priceable (a leg exists to charge it against), but
#:                               ``allow_undefined_risk`` is False. The remedy is the SETTING, not
#:                               the ceiling -- this is F12's whole point.
#:   * ``_CAUSE_UNPRICEABLE`` -- nothing could be priced no matter what is granted: an
#:                               UNMEASURABLE quote, a builder that declined the candidate, or an
#:                               UNBOUNDED loss with no leg to attribute it to (a short stock
#:                               leg). No setting or budget fixes this; only better data does.
#:
#: Compared by identity, never by equality -- see ``_OffScale`` for why that convention exists in
#: this module.
_CAUSE_PRICED = "PRICED"
_CAUSE_PERMISSION = "PERMISSION"
_CAUSE_UNPRICEABLE = "UNPRICEABLE"


@dataclass(frozen=True)
class _Charge:
    """One candidate's answer from ``_chargeable_max_loss``: what it costs, and why, if it can't
    be afforded.

    ``amount`` IS THE ONLY FIELD THE ELIGIBILITY COMPARISON MAY READ. It is exactly what the old
    bare ``float`` return used to be -- ``math.inf`` for every refusal, a real figure otherwise --
    so ``charge.amount <= ctx.max_loss_ceiling`` is byte-identical to the pre-F12 comparison for
    every admitted candidate. ``cause`` and ``would_be_charge`` exist purely for
    ``_ceiling_reason`` to explain a refusal; nothing upstream of it may consult them.

    ``would_be_charge`` IS THE ONE PLACE THIS CARRIES A NUMBER ``amount`` DOES NOT. For a
    ``_CAUSE_PERMISSION`` candidate it is the synthetic assignment cost that WOULD have been
    charged had ``allow_undefined_risk`` been True -- computed here, for free, because the leg
    list and the loss are already in hand; recomputing it from ``_ceiling_reason`` would be a
    second builder pass. It lets ``_ceiling_reason`` test the EXACT question a mixed refusal
    needs answered -- "does this candidate already fit the ceiling once permission is granted,
    with no ceiling change at all" (``would_be_charge <= ctx.max_loss_ceiling``) -- without ever
    calling ``structure_fn`` twice to find out. None for every other cause: a ``_CAUSE_PRICED``
    charge already carries its number in ``amount``, and a ``_CAUSE_UNPRICEABLE`` one has none
    to give under any remedy.
    """

    amount: float
    cause: str
    would_be_charge: Optional[float] = None


def _chargeable_max_loss(c: OptionContract, ctx: PolicyContext) -> _Charge:
    """This candidate's charge against ``ctx.max_loss_ceiling``, and why it is what it is.

    ONLY EVER CALLED WITH A ``structure_fn`` PRESENT -- ``eligible`` returns before this when
    there is none. See its docstring for why an absent seam disables the filter instead of
    emptying the chain.

    THREE STATES, THREE ANSWERS -- collapsing any two is a bug:

      * MEASURED     -> ``_CAUSE_PRICED`` at the measured amount.
      * UNBOUNDED    -> priceable (a leg to charge the assignment against) and
                        ``allow_undefined_risk`` True -> ``_CAUSE_PRICED`` at the synthetic
                        assignment cost. Priceable but the setting is False -> ``_CAUSE_PERMISSION``,
                        infinite, carrying the synthetic as ``would_be_charge`` -- refused for
                        lack of PERMISSION, not for lack of a number. Not priceable at all (no
                        leg to attribute the risk to) -> ``_CAUSE_UNPRICEABLE``, infinite, no
                        matter what the setting says. The distinction between the first two is
                        not cosmetic: it is the difference between an account that forbids naked
                        shorts and a naked short nobody can price, and a single "excluded" branch
                        would report the second when the first is true -- F12.
      * UNMEASURABLE -> ``_CAUSE_UNPRICEABLE``, infinite. A broken quote is NEVER priced by rule,
                        exactly as ``_risk_denominator`` refuses to. The arbitrage vertical's
                        short call has a perfectly usable strike, so the synthetic figure is
                        right there for the taking; taking it would launder a crossed quote into
                        a budget.

    THIS DELIBERATELY DISAGREES WITH ``_reward_to_risk`` ON THE UNMEASURABLE STATE, AND THE
    DISAGREEMENT IS NOT AN OVERSIGHT TO UNIFY. Given an unmeasurable loss, ``rr`` DEMOTES the
    candidate and KEEPS it -- because one crossed quote must not disable a gene for every
    candidate beside it, and a ranking that loses one row still ranks. This REMOVES it, because a
    budget has no equivalent of "scores worst": there is no charge that is both unknown and
    affordable. Same input, opposite verdicts, because ranking can absorb an unknown and spending
    cannot.

    A BUILDER THAT DECLINES THE CANDIDATE ALSO GETS ``_CAUSE_UNPRICEABLE``. When ranking, a
    declined candidate is one missing value among peers and scores worst; a budget has no "worst"
    that is also affordable, and a structure the builder could not complete has no max loss to
    charge, no matter what any setting says.

    THE SYNTHETIC IS ``_risk_denominator``'s, DELIBERATELY, AND THIS DEVIATES FROM THE DESIGN.
    Design section 8.3 words the budgeting substitute for undefined risk as ``spot * 100``; this
    charges ``strike * multiplier * ratio`` instead. Two reasons. ``spot`` does not vary across
    the candidates of one pick, so a spot-based charge would admit and refuse the whole chain
    together and could never re-pick a cheaper strike -- which is the entire purpose of this
    filter. And the codebase now has exactly ONE notion of what an unbounded structure risks;
    a second synthetic for the same shape is how the two drift apart, with nothing downstream
    able to say which figure a given refusal used. It is computed HERE, ONCE, whether or not
    permission is granted, precisely so a permission refusal can still report the number that
    would have applied -- see ``would_be_charge``.

    ``amount`` IS ``inf`` RATHER THAN ``None`` ON EVERY REFUSAL because the caller's test is a
    single ``<= ceiling`` comparison, and ``inf <= x`` is False for every finite ``x``. An
    Optional would put the fail-closed rule in the caller, where forgetting the ``is None``
    branch fails OPEN.
    """
    # NOTE: called WITHOUT a try/except, exactly as ``_profit_and_risk`` calls it, and for the
    # same reason. Declining is None or an empty list; an exception is a defect in the builder
    # and propagates. See ``PolicyContext.structure_fn``.
    legs = ctx.structure_fn(c)
    if not legs:
        return _Charge(math.inf, _CAUSE_UNPRICEABLE)
    loss = max_loss(legs)
    if loss.state == MEASURED:
        return _Charge(float(loss.amount), _CAUSE_PRICED)
    if loss.state == UNBOUNDED:
        # PERMISSION IS NOT A NUMBER. A short stock leg's risk is permitted here and still
        # unpriceable, so ``_risk_denominator`` declines and the candidate is refused anyway --
        # computed unconditionally, on purpose: see the docstring's note on ``would_be_charge``.
        synthetic = _risk_denominator(legs, loss)
        if synthetic is None:
            return _Charge(math.inf, _CAUSE_UNPRICEABLE)
        if not ctx.allow_undefined_risk:
            return _Charge(math.inf, _CAUSE_PERMISSION, would_be_charge=synthetic)
        return _Charge(synthetic, _CAUSE_PRICED)
    return _Charge(math.inf, _CAUSE_UNPRICEABLE)


@dataclass(frozen=True)
class SelectionRefusal:
    """WHY the policy chose nothing. A reason, never a silent zero.

    ``pick`` returns ``None`` for four different reasons and an operator can act on none of them
    without knowing which: an empty chain is a DATA outage, an empty box is a mis-set band, an
    unaimable strike method is a missing input on the recommendation, and a binding ceiling is a
    budget smaller than the cheapest thing in the box. Design section 9 is explicit that "the
    sleeve stopped trading" must be diagnosable, and an ``Optional[OptionContract]`` cannot say
    any of it.

    NOT ``option_request.StructureRefusal``, WHICH IT IS OTHERWISE A COPY OF. That one carries
    the ``OptionStructureRequest`` the refusal belongs to, and this module is pure selection: it
    is handed a candidate list and a ``PolicyContext``, and has never seen a request. The caller
    that has both attaches one to the other. The PHRASES are deliberately the same registry --
    ``validate_refusal_phrase`` is shared, not re-implemented -- because a caller greps for a
    phrase and does not care which layer emitted it.
    """

    phrase: str
    detail: str

    def __post_init__(self):
        validate_refusal_phrase(self.phrase)


def _no_candidate_reason(candidates: Sequence[OptionContract],
                         aimable: Sequence[OptionContract],
                         ctx: PolicyContext) -> SelectionRefusal:
    """Why the BOX ended up empty — before any budget was consulted.

    THE CHAIN AND THE BOX ARE SEPARATED because their remedies do not overlap. An empty chain is
    a feed or a DTE window that returned nothing, and widening the delta band will never fix it;
    an empty box is a band nobody can fall inside, and re-fetching the chain will never fix that.

    A CANDIDATE DROPPED FOR A MISSING DELTA GETS NO PHRASE OF ITS OWN, but it does get counted in
    the detail. ``eligible`` excludes it exactly as ``_pick_by`` does, i.e. on the same footing as
    a contract the box rejected, so inventing a separate cause would claim a distinction the
    filter itself does not draw — while an operator staring at "none of 40 fell inside the box"
    still needs to know that 40 of them never carried a delta to be measured with.
    """
    if not candidates:
        return SelectionRefusal(
            phrase=EMPTY_CHAIN_REFUSAL,
            detail="the candidate list handed to the policy was empty")
    dropped = len(candidates) - len(aimable)
    detail = (f"none of {len(candidates)} candidates fell inside the box "
              f"(strike_method={ctx.strike_method!r}, box_min={ctx.box_min}, "
              f"box_max={ctx.box_max}, target={ctx.target})")
    if dropped:
        detail += f"; {dropped} of them carried no usable delta"
    return SelectionRefusal(phrase=EMPTY_BOX_REFUSAL, detail=detail)


def _ceiling_reason(charges: Sequence[_Charge], ctx: PolicyContext) -> SelectionRefusal:
    """Why the BUDGET emptied a box that was not empty. Called only when it did.

    FIVE STEPS, AND ONLY ONE OF THEM IS ABOUT THE CEILING'S SIZE -- F12 is the bug this function
    exists to fix: every other cause used to collapse into ``MAX_LOSS_UNMEASURABLE_REFUSAL`` (or,
    for the exhausted-budget value, into ``BUDGET_CEILING_REFUSAL``), sending the operator to
    re-pull quotes or widen a box when the remedy was a SETTING or nothing was ever going to fit.

    THE ORDER BELOW IS A PRIORITY LADDER, NOT A SEQUENCE OF INDEPENDENT CHECKS, and each step is
    the EXACT answer to "which remedy admits the cheapest candidate" -- not an approximation of
    it, because both numbers a mixed box needs are already sitting in ``charges`` (see
    ``_Charge.would_be_charge``): no second builder pass is needed to compare them precisely.

      1. ``ctx.max_loss_ceiling < MIN_MEASURABLE_LOSS`` -> ``BUDGET_EXHAUSTED_REFUSAL``,
         unconditionally, before any cause is even inspected. A MEASURED charge is always
         strictly greater than ``MIN_MEASURABLE_LOSS`` (``max_loss``'s own invariant), so a
         ceiling below it can never admit ANY priced candidate, on ANY chain, no matter what the
         quotes say or what permission is granted. Reporting a cause below this line would sooner
         or later send an operator to fix a quote or flip a setting and get refused again by a
         budget that was never going to pay for anything.
      2. A ``_CAUSE_PERMISSION`` candidate exists whose ``would_be_charge`` is already
         ``<= ctx.max_loss_ceiling`` -> ``UNDEFINED_RISK_REFUSAL``. CHECKED FIRST, AHEAD OF ANY
         PRICED CANDIDATE, because such a candidate needs NO ceiling change at all -- flipping
         ``allow_undefined_risk`` alone admits it immediately at the ceiling already in place. A
         permission candidate priced at 200 against a ceiling of 300, sitting beside a priced
         candidate at 4800, is a case where "raise the ceiling above 4800" is not merely a worse
         answer than "flip the setting" -- it asks for a budget increase the box does not need at
         all. No comparison against the priced figures is required to see that: a remedy costing
         zero additional ceiling always beats one that costs a raise, whatever the raise's size.
      3. No candidate cleared step 2, but a ``_CAUSE_PRICED`` one exists -> ``BUDGET_CEILING_
         REFUSAL``, at the cheapest such charge. Every permission candidate left at this point
         needs its ``would_be_charge`` to exceed the ceiling too (step 2 would have caught it
         otherwise), so a priced remedy -- a concrete, already-known raise to a specific number --
         is preferred over asking for both a setting flip AND a further ceiling raise to an
         amount that is still unknown until the setting is flipped.
      4. No priced candidate either, but a ``_CAUSE_PERMISSION`` one with a real
         ``would_be_charge`` exists (necessarily still above the ceiling, or step 2 would have
         fired) -> ``UNDEFINED_RISK_REFUSAL``, at the cheapest such would-be charge. This is
         F12's headline case: every exclusion left standing is a want of PERMISSION, and the
         operator is sent to the setting, not to the chain -- even though the setting alone will
         not be enough here; the detail says so.
      5. Otherwise -> ``MAX_LOSS_UNMEASURABLE_REFUSAL``. Nothing left could be priced under any
         remedy: broken quotes, declined builds, or an unbounded loss with no leg to attribute it
         to. No setting or budget fixes this; only better data does.

    THE NUMBERS ARE THE POINT OF STEPS 2, 3 AND 4. 210 against a 200 ceiling is one strike of
    slack; 210 against a 20 ceiling is a structure this sleeve cannot afford at any strike in the
    box. Both are "no", and only the first is worth widening a band for -- and the same is true
    of a permission remedy's synthetic figure against the setting it is gated on.
    """
    if ctx.max_loss_ceiling < MIN_MEASURABLE_LOSS:
        return SelectionRefusal(
            phrase=BUDGET_EXHAUSTED_REFUSAL,
            detail=(f"max_loss_ceiling {ctx.max_loss_ceiling:.2f} is below the "
                    f"{MIN_MEASURABLE_LOSS:.2f} floor every measured max loss clears, so no "
                    f"contract could ever fit it regardless of quotes or permissions "
                    f"({len(charges)} candidates in the box)"))
    would_be = [charge.would_be_charge for charge in charges
               if charge.cause == _CAUSE_PERMISSION and charge.would_be_charge is not None]
    free_permission_fix = [wb for wb in would_be if wb <= ctx.max_loss_ceiling]
    if free_permission_fix:
        return SelectionRefusal(
            phrase=UNDEFINED_RISK_REFUSAL,
            detail=(f"a candidate carrying unbounded loss prices at "
                    f"{min(free_permission_fix):.2f}, which already fits the "
                    f"{ctx.max_loss_ceiling:.2f} ceiling, but allow_undefined_risk is False -- "
                    f"flipping that setting alone admits it, no larger ceiling needed"))
    priced = [charge.amount for charge in charges if charge.cause == _CAUSE_PRICED]
    if priced:
        return SelectionRefusal(
            phrase=BUDGET_CEILING_REFUSAL,
            detail=(f"cheapest chargeable max loss {min(priced):.2f} exceeds ceiling "
                    f"{ctx.max_loss_ceiling:.2f} ({len(charges)} candidates in the box)"))
    if would_be:
        return SelectionRefusal(
            phrase=UNDEFINED_RISK_REFUSAL,
            detail=(f"{len(would_be)} of {len(charges)} candidates in the box carry unbounded "
                    f"loss priceable at {min(would_be):.2f} or more but "
                    f"allow_undefined_risk is False -- permission, not the ceiling, is what "
                    f"refuses them (the ceiling would still need raising to at least "
                    f"{min(would_be):.2f} even after the setting is flipped)"))
    return SelectionRefusal(
        phrase=MAX_LOSS_UNMEASURABLE_REFUSAL,
        detail=(f"no chargeable max loss could be computed for any of {len(charges)} "
                f"candidates in the box under any setting, so none can be shown to fit the "
                f"{ctx.max_loss_ceiling:.2f} ceiling"))


def eligible(candidates: Sequence[OptionContract],
             ctx: PolicyContext) -> List[OptionContract]:
    """The candidates the policy is allowed to choose between.

    THE LIST ONLY. ``_eligible_and_reason`` does the work and also says WHY the list is empty
    when it is; this drops the reason, because the existing callers and the whole no-op test
    suite want a list and nothing else. See ``pick_with_reason`` for the reason.

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

    THE BUDGET CEILING IS THE LAST FILTER AND IT FAILS CLOSED ON LOSS ONLY. There is deliberately
    no max-PROFIT ceiling, now or ever: an unmeasurable profit costs a ranking signal, an
    unmeasurable loss can bankrupt the sleeve. See ``_chargeable_max_loss`` for what each of the
    three loss states is charged.

    NO ``structure_fn`` MEANS THE CEILING DOES NOT APPLY, WHICH IS NOT THE SAME AS THE FAIL-CLOSED
    RULE INSIDE IT, and the difference is which layer is broken. With a closure present, a
    candidate whose own loss cannot be measured sits among peers that WERE measured, so refusing
    it costs one contract. With no closure at all NOTHING can be measured, so fail-closed would
    refuse 100% of every chain -- an untaught builder handed a ceiling would silently stop trading
    while the setting still read as configured, which is precisely the failure ``_in_box`` refuses
    for ``consensus_target``. It is the same asymmetry ``_column_cannot_rank`` already draws: one
    absent value is a defect in THAT candidate, an empty column is a defect in the QUESTION.

    THAT IS SAFE BECAUSE WITH NO CLOSURE THE FILTER IS INERT -- byte-identical to the pre-ceiling
    pick, so nothing can regress. That argument needs no downstream layer, and it is the only one
    available, because THE DOWNSTREAM LAYER DOES NOT EXIST. An earlier version of this note said
    "sizing measures the real legs and still refuses at contracts = 0", in the present tense, and
    it was false:

      * there is no option-structure triage at all -- ``book_left``, ``instrument_left`` and
        ``structure_cap`` have zero hits repo-wide and live only in the design;
      * ``option_book.admit`` gates whole structures against absolute caps and has no quantity
        field, so it cannot refuse a per-contract charge against a remaining budget;
      * the ONLY place a budget becomes a contract count and refuses at zero is
        ``TradeActions._size_by_cost``, and it divides by PREMIUM or RESERVE
        (``option_request.SIZING_BASES``), never by max loss.

    FOR A CREDIT SPREAD THOSE ARE NOT THE SAME NUMBER, and that is this feature's own motivating
    case: a 5-wide vertical taken for a 2.90 credit has a premium OUTLAY of nothing and 210
    dollars of max loss, so the refusal this filter's charge implies does not exist even in
    principle today. WHEN A BUILDER IS TAUGHT ITS CLOSURE, THAT GAP MUST BE CLOSED WITH IT --
    this is the paragraph to read at that moment, because teaching the closure is exactly when
    the filter stops being inert.

    Between the two, admitting can lose a trade the ceiling would have saved; refusing would lose
    every trade that builder would ever make. Only the first is recoverable by teaching the
    builder its closure.
    """
    return _eligible_and_reason(candidates, ctx)[0]


def _eligible_and_reason(
        candidates: Sequence[OptionContract],
        ctx: PolicyContext) -> Tuple[List[OptionContract], Optional[SelectionRefusal]]:
    """``eligible``'s rules, plus WHY the result is empty when it is. See ``eligible``.

    THE REASON IS BUILT INSIDE THE PASS THAT ALREADY FILTERED, never by a second one. The charge
    column costs a ``structure_fn`` call plus a full ``max_loss`` scan per candidate (5039us on a
    200-row chain), so a diagnostic that recomputed it would double the cost of exactly the path
    that just refused -- and, as ``payoff_columns`` argues for ``feature_matrix`` and
    ``inapplicable_features``, two independent passes let a stateful closure make the REPORT
    describe numbers the FILTER never saw. Same precedent, same fix: compute once, hand it on.

    THE ORDER OF THE CAUSES IS THE ORDER OF THE FILTERS, and that is what keeps them honest. Each
    reason is produced at the point its own filter emptied the list, so a box that was never
    populated cannot be blamed on a budget it never reached, and a budget that removed real
    contracts cannot be reported as an empty box.
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
        # A CAUSE OF ITS OWN, not an empty box. ``_in_box`` admits every contract under
        # ``consensus_target``, so the box cannot be what emptied this; the remedy is a target
        # price on the recommendation, and no band or budget will ever supply one.
        return [], SelectionRefusal(
            phrase=SELECTION_CONFIG_REFUSAL,
            detail=(f"strike_method={ctx.strike_method!r} yields no target strike "
                    f"(target={ctx.target}, spot={ctx.spot}, target_price={ctx.target_price}), "
                    f"so none of {len(candidates)} candidates can be aimed at"))
    boxed = [c for c in out if _in_box(c, ctx)]
    if not boxed:
        return [], _no_candidate_reason(candidates, out, ctx)
    # RETURN BEFORE ASKING THE BUILDER ANYTHING. The no-op guarantee is not merely that the RESULT
    # is unchanged when no ceiling is set: computing every charge and then comparing it against
    # infinity would give the same list while calling ``structure_fn`` once per candidate on a
    # path that runs per structure, per bar, per symbol -- and would propagate the exception from
    # any builder whose closure raises, taking down picks that never asked for a budget. The
    # reason machinery changes nothing about that: it never charges a candidate the filter did
    # not already have to charge.
    if ctx.max_loss_ceiling is None or ctx.structure_fn is None:
        return boxed, None
    charges = [_chargeable_max_loss(c, ctx) for c in boxed]
    kept = [c for c, charge in zip(boxed, charges) if charge.amount <= ctx.max_loss_ceiling]
    if kept:
        return kept, None
    return [], _ceiling_reason(charges, ctx)


def score_all(candidates: Sequence[OptionContract], ctx: PolicyContext,
              policy: SelectionPolicy) -> List[float]:
    """The weighted score of each candidate. Higher wins."""
    weights = {"box_center": policy.w_box_center, "premium": policy.w_premium,
               "iv": policy.w_iv, "rvol": policy.w_rvol, "spread": policy.w_spread,
               "profit": policy.w_profit, "rr": policy.w_rr}
    # SKIP ZERO WEIGHTS ENTIRELY -- do not compute the feature and do not multiply by it.
    #
    # `0.0 * x` looks like it removes a disabled gene from the decision. It does not: `0.0 * nan`
    # is `nan`, so ONE non-finite value in ANY of the source fields turned every score into NaN,
    # `min()` compared NaN tuples (every comparison False) and returned candidate #0 BY LIST
    # ORDER. Four of those fields -- iv, volume, bid, ask -- are ones the selector this must
    # imitate never reads at all, so the no-op guarantee was silently conditional on data hygiene
    # in columns nobody was checking.
    #
    # Not computing the feature is also what makes the default policy affordable. Measured on a
    # 200-contract chain, 2000 iterations, with the five ORIGINAL genes active: the legacy
    # selector is 27.1 us/call, all five active is 388.8 us, and default weights is 99.5 us -- so
    # the skip is worth 3.9x. The residual 3.7x over legacy is the cost of the mechanism itself
    # (building and normalising the box_center column) and is the number to attack if the GA hot
    # path ever needs it; it is per structure, per bar, per symbol.
    #
    # THE SKIP MATTERS MORE NOW THAN IT DID WHEN THAT WAS MEASURED, and here is the figure.
    # `profit` and `rr` are not field reads: each costs a `structure_fn` call plus a full
    # `max_profit` and `max_loss` scan per candidate. Same 200-row chain, both payoff columns:
    # 5039 us -- about 13x the 388.8 us of all five original genes together, and 186x the 27.1 us
    # legacy selector. That ratio is what makes the zero-weight skip load-bearing rather than
    # merely nice: at default weights neither column is computed at all, and `feature_matrix`
    # shares ONE payoff pass between the two so that switching both genes on costs 5039 us
    # rather than 10078 us.
    #
    # INDEXING `weights[name]` RATHER THAN `.get(name)` IS DELIBERATE. A feature added to
    # FEATURE_NAMES without a matching weight is a KeyError on the next pick, which is loud and
    # immediate; with a default it would be a feature that exists, normalises, and is then
    # silently never scored -- the dead gene this module keeps legislating against.
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

    RETURNS THE CONTRACT ONLY, unchanged. ``pick_with_reason`` is the same computation and also
    says why there was nothing to return.
    """
    return pick_with_reason(candidates, ctx, policy)[0]


def pick_with_reason(
        candidates: Sequence[OptionContract], ctx: PolicyContext,
        policy: SelectionPolicy) -> Tuple[Optional[OptionContract], Optional[SelectionRefusal]]:
    """``pick``, plus a ``SelectionRefusal`` whenever it chose nothing.

    THE SEAM ``_resolve()`` WILL USE. ``pick`` has no production caller yet, so this is designed
    for the one that is coming rather than retrofitted around one that exists; it returns a pair
    instead of raising because a refusal is DATA the risk manager triages beside every other
    candidate on the bar, not an exception that unwinds the bar.

    A SEPARATE ENTRY POINT RATHER THAN A WIDER RETURN ON ``pick``, because the no-op guarantees
    of Tasks 1-5 all rest on ``pick`` being the very function the recorded-chain tests pin. Its
    signature and its result are untouched here; the reason costs a caller one extra unpacking
    and costs the existing callers nothing at all.

    EXACTLY ONE OF THE TWO IS EVER SET. A contract and a reason together would teach callers to
    read the contract and ignore the reason, which is how "a reason, never a silent drop" decays
    into a field nobody looks at.
    """
    cands, refusal = _eligible_and_reason(candidates, ctx)
    if not cands:
        return None, refusal
    scores = score_all(cands, ctx, policy)
    best = min(range(len(cands)),
               key=lambda i: (-scores[i], cands[i].strike, cands[i].expiry))
    return cands[best], None
