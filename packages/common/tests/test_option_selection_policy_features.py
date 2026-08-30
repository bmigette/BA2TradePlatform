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
from ba2_common.core.option_selection_policy import (
    FEATURE_NAMES,
    PAYOFF_FEATURES,
    PolicyContext,
    SelectionPolicy,
    _profit_and_risk,
    feature_matrix,
    inapplicable_features,
    pick,
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
