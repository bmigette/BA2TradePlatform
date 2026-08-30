"""The two payoff-derived selection features, and the applicability rule that keeps them from
becoming dead genes.

WHY THESE TWO FEATURES NEED A SEAM AT ALL. Every other feature reads a field off ONE contract.
``max_profit`` and ``max_loss`` are properties of a WHOLE STRUCTURE, and for a vertical the policy
picks one leg while the builder derives the wing afterwards -- so at the moment ``pick`` runs
there is no structure to measure. ``PolicyContext.structure_fn`` is the builder's own closure that
completes a candidate into its leg list: the policy never learns structure shapes, the builder
never learns scoring.

THE LOAD-BEARING RULE THIS FILE EXISTS TO PIN. An unmeasurable max PROFIT must never narrow the
search. UNBOUNDED profit is the DEFINING property of a long call, not a defect in it, and the GA
grid exists partly to measure whether long premium pays -- so a feature that quietly scored every
long call worst would answer that question by construction instead of by measurement. Hence the
asymmetry these tests pin from both sides:

  * a WHOLLY unrankable column is INERT -- every candidate scores the same, the ranking is
    untouched, and ``inapplicable_features`` says so out loud rather than letting the weight
    become a gene the GA can never move;
  * a value missing on ONE candidate whose peers have real ones still fails CLOSED and scores
    worst, exactly as every other feature does.

AN UNBOUNDED LOSS IS NOT ONE OF THOSE CASES, AND THE SYMMETRY IS BROKEN ON PURPOSE. A naked
short's true reward-to-risk is ``profit / infinity -> 0``, so LOW is the honest answer where
UNKNOWN is not, and ``rr`` divides by a synthetic assignment cost instead of refusing. Unbounded
PROFIT has no such stand-in: there is no honest finite number for "the upside is open-ended", so
that side really does go inert.
"""
from datetime import date

import pytest

from ba2_common.core.option_payoff import MEASURED, PayoffLeg, max_loss
from ba2_common.core.option_request import (
    BUDGET_CEILING_REFUSAL,
    EMPTY_BOX_REFUSAL,
    EMPTY_CHAIN_REFUSAL,
    MAX_LOSS_UNMEASURABLE_REFUSAL,
    SELECTION_CONFIG_REFUSAL,
)
from ba2_common.core.option_selection_policy import (
    FEATURE_NAMES,
    PAYOFF_FEATURES,
    PolicyContext,
    SelectionPolicy,
    SelectionRefusal,
    _OFF_SCALE,
    _profit_and_risk,
    _reward_to_risk,
    eligible,
    feature_matrix,
    inapplicable_features,
    payoff_columns,
    pick,
    pick_with_reason,
)
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight, OrderDirection

TODAY = date(2026, 1, 1)
EXPIRY = date(2026, 2, 20)


def c(strike, *, bid=1.0, ask=1.2, delta=0.30, right=OptionRight.CALL):
    """One chain row. ``bid=None, ask=None`` makes ``mid`` None, i.e. an unpriceable leg."""
    return OptionContract(
        symbol=f"X{int(strike * 1000):08d}{EXPIRY:%y%m%d}", underlying="X",
        option_type=right, strike=float(strike), expiry=EXPIRY, bid=bid, ask=ask,
        last=None, implied_volatility=0.25, delta=delta, volume=100)


def ctx(**kw):
    base = dict(strike_method="delta", target=0.30, spot=100.0, option_type=OptionRight.CALL,
                today=TODAY)
    base.update(kw)
    return PolicyContext(**base)


def credit_vertical(width):
    """Completes each candidate into a short call spread ``width`` points wide.

    Both sides bounded, so both features are applicable -- the baseline case the other closures
    are read against.
    """
    def _fn(cand):
        return [PayoffLeg(kind="call", side=OrderDirection.SELL, premium=cand.mid,
                          strike=cand.strike),
                PayoffLeg(kind="call", side=OrderDirection.BUY, premium=0.10,
                          strike=cand.strike + width)]
    return _fn


def long_call(cand):
    """Unbounded PROFIT, bounded loss (the debit). Kills the ``profit`` column, and with it
    ``rr``, whose numerator it is."""
    return [PayoffLeg(kind="call", side=OrderDirection.BUY, premium=cand.mid,
                      strike=cand.strike)]


def naked_short_call(cand):
    """Bounded PROFIT (the credit), UNBOUNDED loss -- the synthetic-denominator path.

    A naked short PUT does NOT reach that path: ``upside_slope`` is zero for a put-only
    structure, so ``max_loss`` measures it at ``(strike - credit) * 100``. Only a short CALL (or
    short stock) makes the payoff fall without limit, which is the sole way ``max_loss`` returns
    UNBOUNDED.
    """
    return [PayoffLeg(kind="call", side=OrderDirection.SELL, premium=cand.mid,
                      strike=cand.strike)]


def naked_short_put(cand):
    """The cash-secured put: bounded on BOTH sides, so ``rr`` is a real measured ratio."""
    return [PayoffLeg(kind="put", side=OrderDirection.SELL, premium=cand.mid,
                      strike=cand.strike)]


def short_stock(cand):
    """Unbounded loss with NO strike to attribute it to -- the case that must NOT be guessed."""
    return [PayoffLeg(kind="stock", side=OrderDirection.SELL, premium=cand.mid, strike=None)]


def short_stock_with_a_bookkeeping_strike(cand):
    """The same unbounded shape, but carrying a strike that means nothing.

    ``validate_legs`` PERMITS this: its strike checks sit under ``kind in _OPTION_KINDS``, so a
    stock leg's ``strike`` field is never inspected and any value survives. A builder recording
    the strike it hedged around, or reusing one leg dataclass for every kind, produces exactly
    this. The number is bookkeeping, not an assignment price, and nothing may be computed from
    it.
    """
    return [PayoffLeg(kind="stock", side=OrderDirection.SELL, premium=cand.mid, strike=95.0)]


def free_long_call(cand):
    """A long call bought for nothing: profit UNBOUNDED and loss UNMEASURABLE at once.

    THE ONLY SHAPE THAT SEPARATES ``_reward_to_risk``'s TWO GUARDS. A 0-bid/0-ask far strike is
    an ordinary chain row, not a contrived one; ``max_profit`` reports UNBOUNDED off the upside
    slope while ``max_loss`` sees a payoff that is flat at zero and refuses it as a break-even
    quote. Every other structure in this file makes at most one of the two states.
    """
    return [PayoffLeg(kind="call", side=OrderDirection.BUY, premium=cand.mid,
                      strike=cand.strike)]


def call_ratio_spread(cand):
    """Long 1x at the candidate's strike, short 2x five points up.

    THE STRUCTURE THE ``multiplier * ratio`` TERM EXISTS FOR. Net short one call, so the loss is
    UNBOUNDED, and the assignment cost is genuinely DOUBLED -- ``105 * 100 * 2 = 21000``, not the
    10500 a hardcoded ``strike * 100`` would report for precisely the shape whose risk is worst.
    """
    return [PayoffLeg(kind="call", side=OrderDirection.BUY, premium=cand.mid,
                      strike=cand.strike),
            PayoffLeg(kind="call", side=OrderDirection.SELL, premium=1.00,
                      strike=cand.strike + 5.0, ratio=2)]


def two_short_calls(cand):
    """Two separate short calls: unbounded, with NO single assignment cost to name."""
    return [PayoffLeg(kind="call", side=OrderDirection.SELL, premium=cand.mid,
                      strike=cand.strike),
            PayoffLeg(kind="call", side=OrderDirection.SELL, premium=1.00,
                      strike=cand.strike + 10.0)]


def unpayable_debit_spread(cand):
    """A 5-wide call spread bought for more than its width: ``max_profit`` is UNMEASURABLE.

    UNMEASURABLE BY SHAPE, not by quote, which is what makes it the right foil for the
    unbounded-profit case: both are properties of the strikes chosen, and only one of them may
    make the column inert.
    """
    return [PayoffLeg(kind="call", side=OrderDirection.BUY, premium=6.00, strike=cand.strike),
            PayoffLeg(kind="call", side=OrderDirection.SELL, premium=0.10,
                      strike=cand.strike + 5.0)]


def arbitrage_vertical(cand):
    """A 5-wide call spread sold for 6.00: profitable at EVERY price, so ``max_loss`` is
    UNMEASURABLE (a crossed or stale quote) rather than unbounded."""
    return [PayoffLeg(kind="call", side=OrderDirection.SELL, premium=6.00, strike=cand.strike),
            PayoffLeg(kind="call", side=OrderDirection.BUY, premium=0.10,
                      strike=cand.strike + 5.0)]


# A rich candidate and a poor one, so both payoff columns have a real range to normalise over.
RICH = c(100, bid=2.90, ask=3.10, delta=0.30)
POOR = c(105, bid=0.90, ask=1.10, delta=0.15)


# ---------------------------------------------------------------- the seam (structure_fn)

def test_structure_fn_defaults_to_none():
    """None is not 'no structure': it is 'this builder has not been taught the seam yet', and
    the features must go INERT rather than wrong. That is what lets them ship before all 17
    builders supply a closure."""
    assert ctx().structure_fn is None


def test_adding_the_seam_changes_no_existing_pick():
    cands = [c(100, delta=0.30), c(105, delta=0.45), c(110, delta=0.60)]
    with_seam = pick(cands, ctx(structure_fn=credit_vertical(5.0)), SelectionPolicy())
    without = pick(cands, ctx(), SelectionPolicy())
    assert with_seam is without


def test_the_default_policy_never_calls_structure_fn():
    """A zero weight must not merely contribute zero -- the column must not be COMPUTED. Each
    payoff column costs a ``structure_fn`` call plus two payoff scans per candidate, on a path
    that runs per structure, per bar, per symbol."""
    def _explode(_cand):
        raise AssertionError("structure_fn was called with both payoff weights at zero")

    cands = [c(100, delta=0.30), c(105, delta=0.45)]
    assert pick(cands, ctx(structure_fn=_explode), SelectionPolicy()).strike == 100.0


# ---------------------------------------------------------------- the features themselves

def test_profit_and_rr_are_named_features():
    assert "profit" in FEATURE_NAMES and "rr" in FEATURE_NAMES
    assert PAYOFF_FEATURES == ("profit", "rr")


def test_a_richer_credit_scores_higher_on_profit():
    # 290 dollars of max profit against 90: the short 100-strike collects 3.00 against the
    # short 105-strike's 1.00, both paying 0.10 for a 5-wide wing.
    m = feature_matrix([RICH, POOR], ctx(structure_fn=credit_vertical(5.0)), only=["profit"])
    assert m["profit"] == [1.0, 0.0]


def test_a_better_ratio_scores_higher_on_rr():
    # 290/210 = 1.38 against 90/410 = 0.22 -- the same ordering as profit here, which is fine:
    # what is being pinned is that the ratio is COMPUTED, not that it disagrees.
    m = feature_matrix([RICH, POOR], ctx(structure_fn=credit_vertical(5.0)), only=["rr"])
    assert m["rr"] == [1.0, 0.0]


def test_precomputed_payoff_columns_are_reused_by_both_entry_points():
    """Sharing ONE payoff pass is what stops the report describing numbers the ranking never saw.

    ``_column_cannot_rank`` already stops the two entry points drifting in their PREDICATE, but
    computing separately they each invoke ``structure_fn`` afresh -- so a stateful or
    non-deterministic closure could still have them disagree about the INPUT. Counting the calls
    is what proves the parameter is wired rather than accepted and ignored, which is this
    module's recurring way of shipping something decorative. It also halves 5039us of work.
    """
    seen = []

    def _counting(cand):
        seen.append(cand.strike)
        return credit_vertical(5.0)(cand)

    context = ctx(structure_fn=_counting)
    columns = payoff_columns([RICH, POOR], context)
    assert len(seen) == 2
    m = feature_matrix([RICH, POOR], context, only=["profit", "rr"], payoff=columns)
    assert inapplicable_features([RICH, POOR], context, payoff=columns) == ()
    assert len(seen) == 2                      # neither entry point re-invoked the closure
    assert m["profit"] == [1.0, 0.0] and m["rr"] == [1.0, 0.0]


def test_both_payoff_features_are_applicable_for_a_defined_risk_structure():
    assert inapplicable_features([RICH, POOR], ctx(structure_fn=credit_vertical(5.0))) == ()


# ---------------------------------------------------------------- inapplicable, not worst

def test_without_a_structure_fn_both_features_are_inapplicable_not_zero():
    """THE DEAD-GENE GUARD. An untaught builder must lose the feature VISIBLY. A weight that
    silently scores every candidate the same is budget the GA burns on a gene it can never
    move, and nothing in the run says so."""
    assert set(inapplicable_features([RICH, POOR], ctx())) == {"profit", "rr"}


def test_an_unbounded_profit_makes_both_features_inapplicable():
    """A long call reports UNBOUNDED profit at EVERY strike, so the column cannot rank -- and
    ``rr`` falls with it, because a ratio with an unmeasurable numerator is not a number."""
    reported = inapplicable_features([RICH, POOR], ctx(structure_fn=long_call))
    assert set(reported) == {"profit", "rr"}


def test_an_inapplicable_feature_does_not_change_the_ranking():
    """INERT MEANS INERT. This is the assertion the whole load-bearing rule reduces to: a long
    call must not be demoted for having the unbounded upside that is the point of buying it."""
    cands = [c(100, bid=2.90, ask=3.10, delta=0.30), c(105, bid=0.90, ask=1.10, delta=0.45)]
    weighted = SelectionPolicy(w_profit=50.0, w_rr=50.0)
    assert (pick(cands, ctx(structure_fn=long_call), weighted)
            is pick(cands, ctx(), SelectionPolicy()))
    m = feature_matrix(cands, ctx(structure_fn=long_call), only=["profit", "rr"])
    assert m["profit"] == [0.0, 0.0] and m["rr"] == [0.0, 0.0]


# ------------------------------------------- MIXED shapes: the case that needs both rules

def _mixed(unbounded_at, shaper):
    """Completes ONE strike with ``shaper`` and every other candidate into a credit vertical."""
    def _fn(cand):
        if cand.strike == unbounded_at:
            return shaper(cand)
        return credit_vertical(5.0)(cand)
    return _fn


# The long call is deliberately the DEFAULT winner (delta dead on the 0.30 target) and the
# poorest thing in the set on profit. If the column ranks at all, the pick moves off it.
MIXED_VERTICAL_RICH = c(95, bid=3.90, ask=4.10, delta=0.50)
MIXED_LONG_CALL = c(100, bid=2.90, ask=3.10, delta=0.30)
MIXED_VERTICAL_POOR = c(105, bid=0.90, ask=1.10, delta=0.10)
MIXED = [MIXED_VERTICAL_RICH, MIXED_LONG_CALL, MIXED_VERTICAL_POOR]


def test_one_unbounded_profit_among_measurable_peers_makes_the_whole_column_inert():
    """THE CASE THE COLUMN RULE ALONE COULD NOT DECIDE, and the reason UNBOUNDED and
    UNMEASURABLE cannot share a representation.

    Collapse both to "missing" and this set is a Rule 2 violation reached through the mechanism
    Rule 3 mandates: the long call is the only candidate without a number, so it alone scores
    ``_WORST`` while its peers score real values, ``inapplicable_features`` reports nothing
    wrong, and ``w_profit`` quietly deletes long premium from a mixed chain. Measured before the
    fix: profit column ``[0.0, 1.0, 0.0]`` and the pick moved off the long call.

    You cannot min-max infinity against 300 on a normalised scale. Scoring it worst DEMOTES it;
    scoring it best OVER-promotes it, so ``w_profit`` would always take the long call instead.
    Neither is a measurement, so the column declines to rank at all.
    """
    context = ctx(structure_fn=_mixed(100.0, long_call))
    assert set(inapplicable_features(MIXED, context)) == {"profit", "rr"}
    m = feature_matrix(MIXED, context, only=["profit", "rr"])
    assert m["profit"] == [0.0, 0.0, 0.0]
    assert m["rr"] == [0.0, 0.0, 0.0]


@pytest.mark.parametrize("policy", [SelectionPolicy(w_profit=5.0), SelectionPolicy(w_rr=5.0)])
def test_a_long_call_among_verticals_is_not_demoted_by_either_payoff_weight(policy):
    """The behavioural half of the test above: an inert column cannot move the pick, so the
    long call still wins on the box centre exactly as it does at default weights."""
    context = ctx(structure_fn=_mixed(100.0, long_call))
    assert pick(MIXED, ctx(), SelectionPolicy()) is MIXED_LONG_CALL      # the baseline
    assert pick(MIXED, context, policy) is MIXED_LONG_CALL


FREE_LONG_CALL = c(100, bid=0.0, ask=0.0, delta=0.30)
FREEBIE = [MIXED_VERTICAL_RICH, FREE_LONG_CALL, MIXED_VERTICAL_POOR]


def test_a_missing_denominator_beats_an_off_scale_numerator():
    """THE GUARD PRECEDENCE IN ``_reward_to_risk``, pinned directly because it is the single
    most-argued line in this design and swapping it is otherwise invisible.

    A long call bought for 0.00 is UNBOUNDED on profit and UNMEASURABLE on loss simultaneously,
    so it is the only input where the two guards disagree:

        risk-first (current) -> None        this candidate fails closed, the column keeps ranking
        profit-first         -> _OFF_SCALE  the whole column goes inert

    Current order is right: an UNMEASURABLE loss is a BROKEN QUOTE, and one crossed market must
    not be able to disable a gene for every candidate beside it. The price of that choice is
    real and is asserted below -- see the companion test.
    """
    assert _reward_to_risk(_OFF_SCALE, None) is None
    # ...and the same at the column level, which is where it changes a pick.
    context = ctx(structure_fn=_mixed(100.0, free_long_call))
    assert _profit_and_risk(FREE_LONG_CALL, context) == (_OFF_SCALE, None)
    assert inapplicable_features(FREEBIE, context) == ("profit",)      # rr stays LIVE
    m = feature_matrix(FREEBIE, context, only=["profit", "rr"])
    assert m["profit"] == [0.0, 0.0, 0.0]      # off-scale present -> inert
    assert m["rr"] == [1.0, 0.0, 0.0]          # 390/110, freebie fails closed, 90/410


def test_the_precedence_accepts_demoting_a_long_call_on_a_broken_quote():
    """THE PRICE OF THE CHOICE ABOVE, asserted rather than argued.

    On this input the long call IS demoted on ``rr`` while its peers score real ratios -- the
    thing the governing constraint forbids. It arrives through the candidate's own unmeasurable
    LOSS rather than through its unbounded profit, but the effect on the pick is identical, and
    a design note that only lists what a choice prevents is half a note.

    Accepted because the alternative is worse in kind, not merely in degree: one stale or
    crossed quote anywhere in the chain would take ``rr`` inert for every candidate beside it,
    silently converting a data outage into a disabled gene. A demotion is confined to the
    candidate whose quote is broken.
    """
    context = ctx(structure_fn=_mixed(100.0, free_long_call))
    assert pick(FREEBIE, ctx(), SelectionPolicy()) is FREE_LONG_CALL          # the baseline
    assert pick(FREEBIE, context, SelectionPolicy(w_rr=5.0)) is MIXED_VERTICAL_RICH
    # ...while `profit`, which reads the UNBOUNDED state rather than the broken quote, does not
    # move the pick at all. The two features part company on the very same candidate.
    assert pick(FREEBIE, context, SelectionPolicy(w_profit=5.0)) is FREE_LONG_CALL


def test_a_stock_leg_carrying_a_bookkeeping_strike_is_not_priced_from_it():
    """The ``kind != "call"`` clause, pinned on its OWN merits.

    Without it this structure reports a denominator of 9500 invented from a stock leg's
    bookkeeping strike. The clause was reachable only via ``short_stock``, whose strike is None
    and which the ``not leg.strike`` test catches one clause earlier -- so removing it changed
    nothing observable, which is this module's recurring way of shipping a decorative guard.

    A stock leg has no strike to be assigned at. Whatever number the field carries, it is not a
    price, and reading it would put an invented figure on the same scale as measured ones.
    """
    context = ctx(structure_fn=short_stock_with_a_bookkeeping_strike)
    assert _profit_and_risk(RICH, context)[1] is None
    assert inapplicable_features([RICH, POOR], context) == ("rr",)
    assert feature_matrix([RICH, POOR], context, only=["profit"])["profit"] == [1.0, 0.0]


def test_one_unmeasurable_profit_among_measurable_peers_still_fails_closed():
    """The other half of the partition: UNMEASURABLE does NOT make the column inert.

    A debit spread bought for more than its width cannot pay -- a defect in the strikes the
    builder chose, not a shape whose value is off the scale -- so it loses to peers that can,
    and the column keeps ranking. Same mixed-shape set, opposite verdict, which is the whole
    reason the two states are now represented differently.
    """
    context = ctx(structure_fn=_mixed(100.0, unpayable_debit_spread))
    assert "profit" not in inapplicable_features(MIXED, context)
    m = feature_matrix(MIXED, context, only=["profit"])
    assert m["profit"] == [1.0, 0.0, 0.0]      # rich vertical, dud, poor vertical
    assert pick(MIXED, context, SelectionPolicy(w_profit=5.0)) is MIXED_VERTICAL_RICH


# ------------------------------------------- the synthetic denominator for unbounded risk

def test_a_naked_short_can_rank_on_rr_because_unbounded_risk_is_priced_not_refused():
    """An UNBOUNDED max loss gets a SYNTHETIC denominator: the assignment cost of the leg that
    carries the risk, ``strike x the contract multiplier``.

    A naked short's true reward-to-risk is ``profit / infinity -> 0``, so LOW is the honest
    answer, not UNKNOWN. Refusing to rank it would hide the very comparison the grid is meant to
    make -- how undefined-risk premium selling scores against defined-risk premium selling --
    and this reuses the substitution the design already makes when undefined risk is permitted
    rather than inventing a second convention.
    """
    context = ctx(structure_fn=naked_short_call)
    assert inapplicable_features([RICH, POOR], context) == ()
    m = feature_matrix([RICH, POOR], context, only=["profit", "rr"])
    assert m["profit"] == [1.0, 0.0]
    # 300/10000 = 0.030 against 100/10500 = 0.0095: ranked, not refused.
    assert m["rr"] == [1.0, 0.0]


def test_a_naked_short_put_ranks_on_rr_without_any_substitution():
    """A cash-secured put never reaches the synthetic path at all, and that is worth pinning.

    ``upside_slope`` is zero for a put-only structure, so ``max_loss`` MEASURES it at
    ``(strike - credit) * 100`` -- 8600 here, NOT the 9000 a strike-based substitution would
    produce. A reader who assumes "naked short" implies "unbounded" will mis-read every branch
    downstream; only a short CALL or short stock makes the payoff run away.

    ASSERTED ON THE AMOUNT, NOT ON THE RANKING, and that is the whole point of the test. An
    earlier version compared normalised columns and pinned NOTHING: measured (8600) and
    synthetic (9000) denominators cannot be told apart that way, because for a short put both
    ``credit / (K - credit)`` and ``credit / K`` are monotone in ``K / credit``, so they order
    every candidate set IDENTICALLY. No choice of strikes can make them disagree. Only the
    number itself distinguishes them.
    """
    cand = c(90, bid=3.90, ask=4.10, right=OptionRight.PUT)      # 4.00 credit on the 90 put
    legs = naked_short_put(cand)
    loss = max_loss(legs)
    assert loss.state == MEASURED and loss.amount == pytest.approx(8600.0)
    assert _profit_and_risk(cand, ctx(structure_fn=naked_short_put)) == (
        pytest.approx(400.0), pytest.approx(8600.0))


def test_a_naked_short_scores_far_below_a_defined_risk_spread_on_rr():
    """The synthetic denominator has to be BIG or the substitution would flatter the very
    structures it is meant to price honestly.

    Both shapes in ONE candidate set, because the columns are min-max normalised WITHIN the set
    and a ratio compared across two separate calls would be comparing two rescalings. The 90
    strike is a naked short call (400 / 9000 = 0.044, the synthetic path); the 100 strike is a
    5-wide credit vertical (200 / 300 = 0.67, measured). The ordering is the assertion; the
    arithmetic is here so a reader can check it.
    """
    def _short_call_or_vertical(cand):
        return naked_short_call(cand) if cand.strike == 90.0 else credit_vertical(5.0)(cand)

    short = c(90, bid=3.90, ask=4.10, delta=0.30)
    spread = c(100, bid=1.90, ask=2.10, delta=0.30)
    m = feature_matrix([short, spread], ctx(structure_fn=_short_call_or_vertical), only=["rr"])
    assert m["rr"] == [0.0, 1.0]


def test_rr_is_not_a_rescaling_of_profit():
    """THE COLLINEARITY GUARD, and the reason the SYNTHETIC denominator is ``strike``-based and
    not ``spot``-based.

    ``spot`` is the same for every candidate in one pick, so a spot-based denominator would make
    ``rr`` a pure rescale of ``profit``; min-max normalisation would then emit two IDENTICAL
    columns and ``w_rr`` would be perfectly collinear with ``w_profit`` -- two genes searching
    one dimension, which is the dead-gene failure this whole design guards against.

    RUN THROUGH THE UNBOUNDED PATH ON PURPOSE. A measured denominator varies per candidate
    whatever convention is chosen, so a short-put version of this test would pass no matter what
    the synthetic branch did and would guard nothing. These are naked short CALLS, where the
    denominator is the invented one: the 100-strike collects the larger credit (420 > 400) but
    costs more to be assigned on (10500 > 9000), so it wins on ``profit`` and LOSES on ``rr``.
    An outright flip, not a nuance -- and a flip that a constant denominator cannot produce.
    """
    near = c(90, bid=3.90, ask=4.10)      # 400 profit / 9000 synthetic risk = 0.0444
    far = c(100, bid=4.10, ask=4.30)      # 420 profit / 10000 synthetic risk = 0.0420
    m = feature_matrix([near, far], ctx(structure_fn=naked_short_call), only=["profit", "rr"])
    assert m["profit"] == [0.0, 1.0]
    assert m["rr"] == [1.0, 0.0]


def test_the_denominator_counts_the_multiplier_and_the_ratio():
    """A 1x2 call ratio spread owes TWO contracts on assignment, and the denominator says so.

    Pinned as an AMOUNT because a hardcoded ``strike * 100`` produces 10500 here and ranks
    identically to 21000 in any two-candidate set -- the ordering cannot see the factor of two,
    only the number can. Understating the risk of the most dangerous shape on the board is
    exactly the direction of error that matters.

    THAT IMPOSSIBILITY HOLDS ONLY BECAUSE ONE ``structure_fn`` BUILDS ONE SHAPE PER PICK, so the
    ratio is a constant factor across the column and cancels out of every comparison. A closure
    that varied ``ratio`` per candidate would break the tie and a ranking test would bite again
    -- worth knowing before concluding from this comment that ordering can never see the term.
    """
    profit, risk = _profit_and_risk(RICH, ctx(structure_fn=call_ratio_spread))
    assert risk == pytest.approx(21000.0)      # 105 strike x 100 multiplier x 2 ratio
    assert profit == pytest.approx(400.0)      # best case is at the short strike


def test_two_short_calls_have_no_single_assignment_cost():
    """The "exactly one short upside leg" guard, pinned on its OWN merits.

    Both legs are short calls at DIFFERENT strikes, so there is no one assignment cost to name
    and ``rr`` must decline rather than pick a leg. Without this the guard was only ever reached
    via a short stock leg, which the ``kind`` check rejects a line later anyway -- so relaxing
    it to "take the first" changed nothing observable and the guard was decorative.
    """
    context = ctx(structure_fn=two_short_calls)
    assert _profit_and_risk(RICH, context)[1] is None
    assert inapplicable_features([RICH, POOR], context) == ("rr",)
    # ...and profit still ranks: the split stays per-feature.
    assert feature_matrix([RICH, POOR], context, only=["profit"])["profit"] == [1.0, 0.0]


def test_unbounded_risk_with_no_attributable_strike_is_refused_not_guessed():
    """Row 3 of the substitution table, and STILL the per-feature split.

    A short stock leg has unbounded loss and no strike to price it with. An invented denominator
    is worse than an absent feature, so ``rr`` goes inert while ``profit`` -- the credit, which
    IS measured -- keeps ranking. That is the divergence the design turns on: applicability is
    decided per FEATURE, not per structure.
    """
    context = ctx(structure_fn=short_stock)
    assert inapplicable_features([RICH, POOR], context) == ("rr",)
    m = feature_matrix([RICH, POOR], context, only=["profit", "rr"])
    assert m["profit"] == [1.0, 0.0]
    assert m["rr"] == [0.0, 0.0]


def test_an_unmeasurable_loss_is_never_given_a_synthetic_denominator():
    """Row 4. The substitution is for UNBOUNDED only.

    This structure's short call has a perfectly usable strike, so the synthetic denominator is
    RIGHT THERE for the taking -- and must not be taken. UNMEASURABLE means the quote is broken
    (here a 5-wide spread sold for 6.00, i.e. free money), and pricing a broken quote by rule
    launders it into a number that then competes on the same scale as real ones.
    """
    context = ctx(structure_fn=arbitrage_vertical)
    assert inapplicable_features([RICH, POOR], context) == ("rr",)


# ---------------------------------------------------------------- the asymmetry

def test_one_unmeasurable_candidate_fails_closed_without_disabling_the_column():
    """Inapplicable is a property of the STRUCTURE SHAPE, not of one bad quote.

    THREE candidates, not two, and deliberately so: with only one measurable value the range is
    degenerate, the whole column flattens to 0.0, and the assertion below would pass for the
    wrong reason. With two real values the missing one has to be pushed to the worst end by the
    fail-closed rule rather than by arithmetic.
    """
    unpriceable = c(110, bid=None, ask=None, delta=0.20)
    cands = [RICH, POOR, unpriceable]
    context = ctx(structure_fn=credit_vertical(5.0))
    assert inapplicable_features(cands, context) == ()
    m = feature_matrix(cands, context, only=["profit", "rr"])
    assert m["profit"] == [1.0, 0.0, 0.0]     # best, lowest-by-measurement, missing-fails-closed
    # THE SAME ASSERTION ON `rr`. It shares `_maximise` with `profit`, so the behaviour follows
    # -- but "follows from shared code" is not a pin, and the two features diverge elsewhere in
    # this file precisely because they read different states.
    assert m["rr"] == [1.0, 0.0, 0.0]         # 290/210, 90/410, unpriceable


def test_a_builder_that_declines_a_candidate_is_a_missing_value_not_an_error():
    """``structure_fn`` may return None for a candidate it cannot complete -- no wing left on
    the chain, say. That is one missing VALUE, handled like any other, not a crash and not a
    reason to disable the feature for the candidates it could complete."""
    def _declines_the_poor_one(cand):
        if cand.strike >= 105.0:
            return None
        return credit_vertical(5.0)(cand)

    cheaper = c(101, bid=1.90, ask=2.10, delta=0.28)
    cands = [RICH, cheaper, POOR]
    context = ctx(structure_fn=_declines_the_poor_one)
    assert inapplicable_features(cands, context) == ()
    m = feature_matrix(cands, context, only=["profit", "rr"])
    assert m["profit"] == [1.0, 0.0, 0.0]
    assert m["rr"] == [1.0, 0.0, 0.0]         # 290/210, 190/310, declined


def test_a_structure_fn_that_raises_takes_the_pick_down_with_it():
    """DECLINING IS ``None``; RAISING IS A BUG, AND IT PROPAGATES. Deliberate, not an oversight.

    A closure that raises has a defect in the builder -- it read a field that is not there, or
    computed a wing from a None strike. Catching it here would convert a broken builder into
    quietly worse selection on every bar, with nothing in the run naming the builder or the
    line. That is the failure mode this codebase keeps having to remove, so the exception is
    left to travel; ``_OptionEntryAction.execute`` is where an operator-facing refusal belongs.
    """
    def _broken(_cand):
        raise ZeroDivisionError("builder computed a wing from a zero width")

    with pytest.raises(ZeroDivisionError):
        pick([RICH, POOR], ctx(structure_fn=_broken), SelectionPolicy(w_profit=1.0))


# ---------------------------------------------------------------- the weights

def test_the_two_new_weights_default_to_zero_and_keep_is_default_true():
    assert SelectionPolicy().w_profit == 0.0
    assert SelectionPolicy().w_rr == 0.0
    assert SelectionPolicy().is_default


@pytest.mark.parametrize("policy", [SelectionPolicy(w_profit=1.0), SelectionPolicy(w_rr=1.0)])
def test_a_policy_carrying_either_payoff_weight_is_not_default(policy):
    """``is_default`` is what the no-op guarantee is asserted through, so a weight it does not
    look at is a weight that can change a pick while the policy still claims to change
    nothing."""
    assert not policy.is_default


def test_a_live_profit_weight_can_override_the_box_center():
    """The feature is not merely computed -- it can win. Without this, every assertion above is
    satisfied by a column that is calculated and then discarded."""
    near_but_poor = c(100, bid=0.90, ask=1.10, delta=0.30)      # dead on the 0.30 target
    far_but_rich = c(105, bid=2.90, ask=3.10, delta=0.45)       # well off it
    cands = [near_but_poor, far_but_rich]
    context = ctx(structure_fn=credit_vertical(5.0))
    assert pick(cands, context, SelectionPolicy()) is near_but_poor
    assert pick(cands, context, SelectionPolicy(w_profit=5.0)) is far_but_rich


# ------------------------------------------------ the budget ceiling (max_loss_ceiling)
#
# WHAT THIS FILTER IS FOR. Sizing already refuses a structure it cannot afford, but it refuses
# the ONE contract the picker handed it -- it never goes back and takes a cheaper strike that
# WOULD have fitted. The ceiling moves the budget upstream of the choice so the picker cannot
# choose what the budget cannot buy.
#
# EXPENSIVE and CHEAP are 5-wide credit verticals over a 0.10 wing, so the max loss is
# ``(5.00 - credit) * 100``: 410 for the 0.90 credit, 210 for the 2.90 one. The EXPENSIVE one is
# deliberately dead on the 0.30 delta target and therefore the DEFAULT winner, so any test below
# that finds CHEAP has watched the ceiling move a pick rather than agree with one.

EXPENSIVE = c(100, bid=0.90, ask=1.10, delta=0.30)     # (5.00 - 0.90) * 100 = 410 of max loss
CHEAP = c(105, bid=2.90, ask=3.10, delta=0.25)         # (5.00 - 2.90) * 100 = 210 of max loss
PAIR = [EXPENSIVE, CHEAP]


def test_the_ceiling_and_the_permission_default_to_off():
    assert ctx().max_loss_ceiling is None
    assert ctx().allow_undefined_risk is False


def test_a_contract_the_budget_cannot_afford_is_not_merely_sized_down_it_is_not_PICKED():
    """THE POINT OF THE WHOLE FEATURE, asserted as a change of PICK and not of eligibility only.

    Without the ceiling the 410-dollar structure wins on the box centre, sizing then computes
    ``floor(budget / 410) == 0`` and the bar trades NOTHING -- while a 210-dollar structure two
    strikes away fitted the same budget the whole time. With the ceiling the picker never sees
    the one it cannot buy.
    """
    context = ctx(structure_fn=credit_vertical(5.0))
    assert pick(PAIR, context, SelectionPolicy()) is EXPENSIVE          # the baseline
    capped = ctx(structure_fn=credit_vertical(5.0), max_loss_ceiling=300.0)
    assert eligible(PAIR, capped) == [CHEAP]
    assert pick(PAIR, capped, SelectionPolicy()) is CHEAP


def test_the_ceiling_is_inclusive_at_its_own_boundary():
    """A structure risking EXACTLY the ceiling fits. ``<=``, not ``<``.

    Pinned because the off-by-one is invisible in every other test here and the wrong direction
    refuses a structure the budget can pay for to the cent.
    """
    context = ctx(structure_fn=credit_vertical(5.0), max_loss_ceiling=210.0)
    assert eligible(PAIR, context) == [CHEAP]
    assert eligible(PAIR, ctx(structure_fn=credit_vertical(5.0),
                              max_loss_ceiling=209.99)) == []


def test_a_zero_ceiling_refuses_everything_rather_than_disabling_the_filter():
    """0.0 IS THE EXHAUSTED-BUDGET VALUE, not "no budget set", and the two must not share a test.

    The ceiling is ``min(instrument_left, structure_cap)``, so an instrument that has spent its
    allowance arrives here as exactly 0.0. ``is None`` is therefore load-bearing against the
    one-token simplification ``if not ctx.max_loss_ceiling``, which is result-identical for every
    other input in this file and inverts the feature at precisely the moment it matters most: an
    exhausted budget would admit the WHOLE chain. Nothing else here can catch it, because 0.0 is
    the only falsy value the field can legitimately hold.
    """
    context = ctx(structure_fn=credit_vertical(5.0), max_loss_ceiling=0.0)
    assert eligible(PAIR, context) == []
    assert pick(PAIR, context, SelectionPolicy()) is None


def test_the_builder_is_never_asked_about_a_candidate_the_box_rejected():
    """ORDERING, NOT JUST RESULT -- the same class of pin as the ceiling-of-None test above, whose
    technique this file applied to the ``None`` case and forgot to apply to position.

    Running the ceiling BEFORE ``_in_box`` produces an identical list every time, so no assertion
    on the result can tell the two apart. The box is the cheap filter (a field read) and the
    ceiling is the expensive one (a structure build plus a full payoff scan), on a path that runs
    per structure, per bar, per symbol -- and asking the builder about rows that are about to be
    discarded also widens the surface on which a raising closure can take down a pick it was
    never relevant to.
    """
    asked = []

    def _counting(cand):
        asked.append(cand.strike)
        return credit_vertical(5.0)(cand)

    far = c(300, bid=0.01, ask=0.03, delta=0.01)          # outside the delta box, cheap to build
    context = ctx(structure_fn=_counting, max_loss_ceiling=1e9, box_min=0.20, box_max=0.35)
    assert eligible([EXPENSIVE, CHEAP, far], context) == PAIR
    assert 300.0 not in asked
    assert asked == [100.0, 105.0]


def test_a_ceiling_of_none_never_even_asks_the_builder():
    """``max_loss_ceiling=None`` MUST be a provable no-op, not merely a permissive one.

    The whole of ``tests/test_option_selection_policy_noop.py`` rests on ``eligible`` behaving
    exactly as it did, and a filter that computed the charge and then compared it against
    infinity would keep the RESULT identical while calling ``structure_fn`` once per candidate on
    a path that runs per structure, per bar, per symbol -- and would take the pick down entirely
    for the builders whose closures raise. Counting the calls is the only way to tell the two
    apart.
    """
    def _explode(_cand):
        raise AssertionError("structure_fn was called with no max_loss_ceiling set")

    context = ctx(structure_fn=_explode)
    assert eligible(PAIR, context) == PAIR
    assert pick(PAIR, context, SelectionPolicy()) is EXPENSIVE


def test_an_unmeasurable_loss_fails_closed_against_a_ceiling():
    """Cannot prove it fits -> not admitted. A broken quote is NEVER priced by rule.

    The arbitrage vertical's short call has a perfectly usable strike, so the synthetic figure is
    right there for the taking; taking it would launder a crossed quote into a budget. The
    ceiling here is 1e9 -- a billion dollars -- so nothing about SIZE is doing the refusing.
    """
    uncapped = ctx(structure_fn=arbitrage_vertical)
    assert eligible(PAIR, uncapped) == PAIR            # admitted while no budget is asserted
    capped = ctx(structure_fn=arbitrage_vertical, max_loss_ceiling=1e9)
    assert eligible(PAIR, capped) == []
    assert pick(PAIR, capped, SelectionPolicy()) is None


def test_a_builder_that_declines_a_candidate_cannot_be_shown_to_fit():
    """Declining is a missing VALUE when ranking, and ranking tolerates missing values by scoring
    them worst. A BUDGET cannot: there is no "worst" that is also affordable, and a structure the
    builder could not complete has no max loss to charge. Same fail-closed rule as a broken
    quote, reached by a different road."""
    def _declines_the_cheap_one(cand):
        return None if cand.strike >= 105.0 else credit_vertical(5.0)(cand)

    context = ctx(structure_fn=_declines_the_cheap_one, max_loss_ceiling=1e9)
    assert eligible(PAIR, context) == [EXPENSIVE]


def test_an_unbounded_loss_is_charged_and_admitted_when_undefined_risk_is_PERMITTED():
    """A naked short CALL -- the only single-leg shape whose loss is genuinely unbounded. (A naked
    short PUT is BOUNDED: the underlying cannot go below zero, so ``max_loss`` MEASURES it at
    ``(strike - credit) * 100`` and a test written on one would exercise the measured path while
    claiming to test this one.)

    The charge is ``_risk_denominator``'s figure, ``strike x multiplier x ratio`` -- 10000 for the
    100 strike, 10500 for the 105. DELIBERATELY THE SAME SYNTHETIC ``rr`` DIVIDES BY, rather than
    the design's ``spot * 100``: ``spot`` does not vary across candidates, and two different
    synthetic figures for one structure is how they drift apart.

    BOTH DIRECTIONS ARE ASSERTED. A test that only proved the permitted case is admitted would
    let the charge itself rot into a no-op -- "permitted" would come to mean "unmeasured".
    """
    permitted = dict(structure_fn=naked_short_call, allow_undefined_risk=True)
    assert eligible(PAIR, ctx(**permitted, max_loss_ceiling=12000.0)) == PAIR
    # ...tightened between the two synthetics: the 100 strike costs 10000 to be assigned on, the
    # 105 strike 10500, so the ceiling can separate them. It is a real number, not a token.
    assert eligible(PAIR, ctx(**permitted, max_loss_ceiling=10200.0)) == [EXPENSIVE]
    assert eligible(PAIR, ctx(**permitted, max_loss_ceiling=9000.0)) == []


def test_an_unbounded_loss_is_refused_for_want_of_PERMISSION_not_of_size():
    """``allow_undefined_risk`` defaults to False, which is the design's default refusal of
    undefined risk, and the ceiling must not be able to override it in either direction.

    THE CEILING HERE IS A TRILLION DOLLARS. Nothing about size is doing the refusing; flipping
    the one flag on the very same inputs admits both candidates. Collapsing UNBOUNDED into
    "excluded" would make a PERMITTED naked short unselectable while the setting still read as
    ON, and collapsing it the other way would sell undefined risk on an account that forbids it.
    """
    huge = 1e12
    assert eligible(PAIR, ctx(structure_fn=naked_short_call, max_loss_ceiling=huge)) == []
    assert eligible(PAIR, ctx(structure_fn=naked_short_call, max_loss_ceiling=huge,
                              allow_undefined_risk=True)) == PAIR


def test_permitted_undefined_risk_still_needs_a_strike_to_be_charged_against():
    """PERMISSION IS NOT A NUMBER. A short stock leg's loss is unbounded and there is no strike to
    price the assignment with, so ``_risk_denominator`` declines and the candidate is refused even
    though undefined risk is allowed. The alternative is charging the budget an invented figure,
    which is the one thing a budget must never be charged.
    """
    context = ctx(structure_fn=short_stock, allow_undefined_risk=True, max_loss_ceiling=1e12)
    assert eligible(PAIR, context) == []


def test_without_a_structure_fn_the_ceiling_is_inapplicable_rather_than_total():
    """THE DECISION: no seam -> the filter does not apply, and every candidate is admitted.

    NOT the fail-closed rule that governs a candidate whose own loss cannot be measured, and the
    difference is which layer is broken. With a ``structure_fn`` present, an unmeasurable
    candidate sits among peers that WERE measured: refusing it costs one contract. With no
    ``structure_fn`` at all NOTHING can be measured, so fail-closed refuses 100% of every chain
    -- an untaught builder handed a ceiling would silently stop trading altogether while the
    setting read as configured. That is the exact failure ``_in_box`` refuses for
    ``consensus_target``, and the same asymmetry ``_column_cannot_rank`` already draws: one
    absent value is a defect in THAT candidate, an empty column is a defect in the QUESTION.

    IT IS SAFE BECAUSE WITH NO ``structure_fn`` THE FILTER IS INERT -- byte-identical to the
    pre-ceiling pick, so nothing can regress. That argument needs no downstream layer, which is
    just as well, because none exists: there is no option-structure triage today (``book_left``,
    ``instrument_left`` and ``structure_cap`` have zero hits repo-wide and live only in the
    design), and the one sizing step that does exist divides by PREMIUM or RESERVE
    (``option_request.SIZING_BASES``), never by max loss -- so for a credit spread, whose premium
    outlay and max loss are different numbers, an unaffordable pick is NOT caught downstream by
    any "refuses at contracts = 0" mechanism. That claim was retracted from ``eligible``'s own
    docstring (3d676204) as false in the present tense; admitting here can therefore lose a real
    trade the ceiling would have saved, and that is accepted because the alternative -- refusing
    everything for want of a closure -- loses every trade the builder would ever make. Only the
    first is recoverable, by teaching the builder its closure.
    """
    context = ctx(max_loss_ceiling=1.0)                # a one-dollar budget, and it binds nothing
    assert eligible(PAIR, context) == PAIR
    assert pick(PAIR, context, SelectionPolicy()) is EXPENSIVE


def test_there_is_no_max_profit_ceiling():
    """FAILS CLOSED ON LOSS ONLY, pinned so nobody adds the symmetric filter later.

    An unmeasurable PROFIT costs a ranking signal; an unmeasurable LOSS can bankrupt the sleeve.
    ``unpayable_debit_spread`` reports UNMEASURABLE max profit at every strike and a perfectly
    measurable loss of 590 (a 6.00 debit against a 0.10 credit), so a profit-side filter would
    empty the chain here while the loss-side one admits it.
    """
    context = ctx(structure_fn=unpayable_debit_spread, max_loss_ceiling=1000.0)
    assert eligible(PAIR, context) == PAIR


# ------------------------------------------------ WHY nothing was picked (the refusal reason)
#
# ``pick`` returns None for four different reasons and an operator cannot act on any of them
# without knowing which: an empty chain is a data outage, an empty box is a mis-set band, an
# unaimable strike method is a missing input on the recommendation, and a binding ceiling is a
# budget smaller than the cheapest thing in the box. The design's rule (section 9) is that a
# refusal is A REASON, NEVER A SILENT ZERO -- "the sleeve stopped trading" must be diagnosable.
#
# THE PAIR THAT MATTERS IS (ceiling, empty box). They are the two a single ``if not cands`` would
# collapse, and the two whose remedies are furthest apart: widen the band, versus raise the cap
# or aim at cheaper strikes. Every test below that asserts one of them also asserts it is NOT the
# other, so a collapse cannot pass by satisfying half of each.


def test_a_ceiling_that_empties_the_box_is_named_as_the_ceiling_not_as_an_empty_box():
    """THE POINT OF THE TASK. The box held two contracts; the ceiling removed both.

    THE NUMBERS ARE PART OF THE REASON. "no" tells the operator nothing about how far off the
    budget was; 210 against a 200 ceiling is a 5% miss (widen the box one strike) where 210
    against a 20 ceiling is a structure this sleeve can never afford at this size.
    """
    context = ctx(structure_fn=credit_vertical(5.0), max_loss_ceiling=200.0)
    chosen, refusal = pick_with_reason(PAIR, context, SelectionPolicy())
    assert chosen is None
    assert pick(PAIR, context, SelectionPolicy()) is None        # ``pick`` itself is unchanged
    assert refusal.phrase == BUDGET_CEILING_REFUSAL
    assert refusal.phrase != EMPTY_BOX_REFUSAL
    assert "210.00" in refusal.detail        # the cheapest candidate's chargeable max loss
    assert "200.00" in refusal.detail        # the ceiling it exceeded


def test_a_genuinely_empty_box_is_named_as_the_box_not_as_the_ceiling():
    """THE OTHER HALF OF THE PAIR, and it carries a ceiling that WOULD have bound.

    Both candidates sit at deltas 0.25 and 0.30 and the box asks for 0.60-0.90, so nothing
    reaches the ceiling at all -- yet the ceiling is set to 200, which excludes both of them on
    price too. A reporter that ran the ceiling first, or that named whichever filter it happened
    to check last, would blame the budget for a band that was never populated.
    """
    context = ctx(structure_fn=credit_vertical(5.0), max_loss_ceiling=200.0,
                  box_min=0.60, box_max=0.90)
    chosen, refusal = pick_with_reason(PAIR, context, SelectionPolicy())
    assert chosen is None
    assert refusal.phrase == EMPTY_BOX_REFUSAL
    assert refusal.phrase != BUDGET_CEILING_REFUSAL
    assert "0.6" in refusal.detail and "0.9" in refusal.detail


def test_a_successful_pick_carries_no_refusal():
    """A reason is for the ABSENCE of a choice. Reporting one beside a contract would teach every
    caller to read the contract first and ignore the reason, which is how a refusal becomes
    decorative."""
    context = ctx(structure_fn=credit_vertical(5.0), max_loss_ceiling=300.0)
    chosen, refusal = pick_with_reason(PAIR, context, SelectionPolicy())
    assert chosen is CHEAP
    assert refusal is None


def test_an_empty_chain_is_named_as_the_chain_not_as_the_box():
    """A chain that came back empty is a DATA outage; an empty box is a mis-set band. Widening
    the band does nothing for the first and is the whole remedy for the second."""
    context = ctx(structure_fn=credit_vertical(5.0), max_loss_ceiling=200.0)
    chosen, refusal = pick_with_reason([], context, SelectionPolicy())
    assert chosen is None
    assert refusal.phrase == EMPTY_CHAIN_REFUSAL
    assert refusal.phrase not in (EMPTY_BOX_REFUSAL, BUDGET_CEILING_REFUSAL)


def test_an_unaimable_strike_method_names_the_parameter_not_the_box():
    """``consensus_target`` on a recommendation carrying no target price. ``select_single``
    reaches this with its default ``target_price=None``, so it is a live path, and the remedy is
    on the RECOMMENDATION -- nothing about the box or the budget will ever fix it.

    ``_in_box`` returns True for every contract under ``consensus_target``, so the box literally
    cannot be the cause here; naming it would send the operator to the one knob that has no
    effect at all for this method.
    """
    context = PolicyContext(strike_method="consensus_target", today=TODAY, spot=100.0,
                            option_type=OptionRight.CALL, structure_fn=credit_vertical(5.0),
                            max_loss_ceiling=200.0)
    chosen, refusal = pick_with_reason(PAIR, context, SelectionPolicy())
    assert chosen is None
    assert refusal.phrase == SELECTION_CONFIG_REFUSAL
    assert refusal.phrase not in (EMPTY_BOX_REFUSAL, BUDGET_CEILING_REFUSAL)


def test_a_charge_nobody_could_compute_is_not_reported_as_a_ceiling_that_is_too_low():
    """A BILLION-DOLLAR ceiling and nothing fits, because nothing could be PRICED against it.

    ``arbitrage_vertical`` reports UNMEASURABLE max loss at every strike (a crossed quote), which
    ``_chargeable_max_loss`` charges as infinity and the filter therefore refuses. Calling that a
    ceiling that is too low would print "cheapest inf exceeds ceiling 1000000000.00" and send the
    operator to raise a cap that is already a billion dollars, when the defect is in the quote.
    ``MAX_LOSS_UNMEASURABLE_REFUSAL`` already names it exactly, so no new phrase is invented for
    it either.
    """
    context = ctx(structure_fn=arbitrage_vertical, max_loss_ceiling=1e9)
    chosen, refusal = pick_with_reason(PAIR, context, SelectionPolicy())
    assert chosen is None
    assert refusal.phrase == MAX_LOSS_UNMEASURABLE_REFUSAL
    assert refusal.phrase != BUDGET_CEILING_REFUSAL
    assert "inf" not in refusal.detail


def test_reporting_a_reason_never_asks_the_builder_when_no_ceiling_is_set():
    """THE TASK 5 NO-OP GUARANTEE MUST SURVIVE THE REPORTER. ``max_loss_ceiling=None`` still
    reaches the builder zero times -- a diagnostic that charged every candidate just to have a
    number ready in case it needed one would resurrect the exact cost the ceiling's early return
    exists to avoid, and would take the pick down for every builder whose closure raises."""
    def _explode(_cand):
        raise AssertionError("structure_fn was called with no max_loss_ceiling set")

    chosen, refusal = pick_with_reason(PAIR, ctx(structure_fn=_explode), SelectionPolicy())
    assert chosen is EXPENSIVE
    assert refusal is None


def test_the_reason_does_not_buy_a_second_payoff_pass():
    """ONE CHARGE PER CANDIDATE, not one to filter and another to explain.

    ``_chargeable_max_loss`` runs the builder plus a full ``max_loss`` scan; the payoff pass was
    measured at 5039us on a 200-row chain, so a reporter that recomputed it would double the cost
    of the very path that just refused -- and, exactly as ``payoff_columns`` argues for
    ``feature_matrix``/``inapplicable_features``, a second independent pass lets a stateful
    closure make the REPORT describe numbers the FILTER never saw.
    """
    asked = []

    def _counting(cand):
        asked.append(cand.strike)
        return credit_vertical(5.0)(cand)

    context = ctx(structure_fn=_counting, max_loss_ceiling=200.0)
    chosen, refusal = pick_with_reason(PAIR, context, SelectionPolicy())
    assert chosen is None and refusal.phrase == BUDGET_CEILING_REFUSAL
    assert asked == [100.0, 105.0]


def test_a_selection_refusal_cannot_carry_a_free_text_phrase():
    """Same validation as ``StructureRefusal``, against deliberately the SAME registry: a reason
    the caller cannot grep for is a reason nobody reads."""
    SelectionRefusal(phrase=BUDGET_CEILING_REFUSAL, detail="")     # registered: fine
    with pytest.raises(ValueError):
        SelectionRefusal(phrase="the budget was a bit small", detail="")
