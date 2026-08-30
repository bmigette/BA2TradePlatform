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

from ba2_common.core.option_payoff import PayoffLeg
from ba2_common.core.option_selection_policy import (
    FEATURE_NAMES,
    PAYOFF_FEATURES,
    PolicyContext,
    SelectionPolicy,
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
    ``(strike - credit) * 100`` -- 8600 here -- and ``rr`` is an ordinary measured ratio. A
    reader who assumes "naked short" implies "unbounded" will mis-read every branch downstream;
    only a short CALL or short stock makes the payoff run away.
    """
    rich = c(90, bid=3.90, ask=4.10, right=OptionRight.PUT)      # 400 / 8600 = 0.0465
    poor = c(90, bid=0.90, ask=1.10, right=OptionRight.PUT)      # 100 / 8900 = 0.0112
    context = ctx(structure_fn=naked_short_put)
    assert inapplicable_features([rich, poor], context) == ()
    assert feature_matrix([rich, poor], context, only=["rr"])["rr"] == [1.0, 0.0]


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
    assert "profit" not in inapplicable_features(cands, context)
    m = feature_matrix(cands, context, only=["profit"])
    assert m["profit"] == [1.0, 0.0, 0.0]     # best, lowest-by-measurement, missing-fails-closed


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
    assert "profit" not in inapplicable_features(cands, context)
    m = feature_matrix(cands, context, only=["profit"])
    assert m["profit"] == [1.0, 0.0, 0.0]


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
