"""``pick`` applies the box filter, the weights and the tie-break, in that order."""
from datetime import date

import pytest

from ba2_common.core.option_selection_policy import (
    FEATURE_NAMES, PolicyContext, SelectionPolicy, _in_box, feature_matrix, pick)
from ba2_common.core.option_selector import OptionSelectionConfigError
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

TODAY = date(2024, 3, 4)
NEAR = date(2024, 4, 5)
FAR = date(2024, 4, 12)


def c(strike, *, delta=0.30, bid=1.0, ask=1.2, iv=0.25, volume=100, expiry=NEAR):
    return OptionContract(
        symbol=f"X{int(strike * 1000):08d}{expiry:%y%m%d}", underlying="X",
        option_type=OptionRight.CALL, strike=float(strike), expiry=expiry, bid=bid, ask=ask,
        last=None, implied_volatility=iv, delta=delta, volume=volume)


def ctx(**kw):
    base = dict(strike_method="delta", target=0.30, spot=100.0, option_type=OptionRight.CALL,
                today=TODAY)
    base.update(kw)
    return PolicyContext(**base)


def test_empty_candidates_returns_none():
    assert pick([], ctx(), SelectionPolicy()) is None


def test_default_policy_picks_the_delta_closest_to_target():
    cands = [c(105, delta=0.45), c(100, delta=0.31), c(110, delta=0.60)]
    assert pick(cands, ctx(), SelectionPolicy()).strike == 100.0


def test_box_filter_excludes_candidates_outside_the_band():
    cands = [c(100, delta=0.55), c(105, delta=0.20)]
    chosen = pick(cands, ctx(target=0.175, box_min=0.10, box_max=0.25), SelectionPolicy())
    assert chosen.strike == 105.0


def test_degenerate_box_filters_nothing():
    # box_min == box_max is a point target, not a filter. Filtering to delta == 0.30 exactly
    # would leave nothing and the rule would silently stop trading.
    cands = [c(100, delta=0.31), c(105, delta=0.45)]
    chosen = pick(cands, ctx(target=0.30, box_min=0.30, box_max=0.30), SelectionPolicy())
    assert chosen.strike == 100.0


def test_delta_method_excludes_contracts_with_no_delta():
    cands = [c(100, delta=None), c(105, delta=0.90)]
    assert pick(cands, ctx(), SelectionPolicy()).strike == 105.0


def test_delta_method_returns_none_when_no_candidate_has_a_delta():
    assert pick([c(100, delta=None)], ctx(), SelectionPolicy()) is None


def test_delta_method_with_no_target_raises_rather_than_guessing():
    # _pick_by RAISES on this (abs(None)), so raising keeps the no-op. Scoring instead would
    # make every distance unmeasurable, tie every candidate, and quietly hand back the lowest
    # strike -- a real contract chosen for no reason.
    with pytest.raises(OptionSelectionConfigError):
        pick([c(100, delta=0.30)], ctx(target=None), SelectionPolicy())


def test_consensus_target_with_no_target_price_selects_nothing_rather_than_guessing():
    # The same refusal, for the OTHER method that can fail to produce a target. It is the
    # reachable one: ``select_single``'s ``target_price`` defaults to None, so a
    # consensus_target rule on a recommendation that carried no price lands here, and
    # ``_pick_by`` returns None for it (``target_strike`` -> None). Scoring instead would tie
    # every candidate on an unmeasurable distance and hand back the lowest strike -- opening a
    # deep-ITM position where the legacy selector opened nothing at all.
    cands = [c(100, delta=0.30), c(105, delta=0.40)]
    assert pick(cands, ctx(strike_method="consensus_target", target=None, target_price=None),
                SelectionPolicy()) is None


def test_a_weight_can_override_the_box_center_preference():
    # 100 is nearer the 0.30 target; 105 pays far more premium. With premium weighted heavily
    # the policy must prefer 105 — this is the whole point of the mechanism.
    cands = [c(100, delta=0.30, bid=0.10, ask=0.12), c(105, delta=0.40, bid=5.0, ask=5.2)]
    chosen = pick(cands, ctx(), SelectionPolicy(w_premium=5.0))
    assert chosen.strike == 105.0


def test_ties_break_to_the_lowest_strike_then_the_earliest_expiry():
    # Identical on every feature; only strike and expiry differ.
    cands = [c(110, expiry=FAR), c(100, expiry=FAR), c(100, expiry=NEAR)]
    chosen = pick(cands, ctx(strike_method="percent_otm", target=0.0), SelectionPolicy())
    assert (chosen.strike, chosen.expiry) == (100.0, NEAR)


# --- added after the 2026-08-27 code-quality review ------------------------------------------
#
# Mutation testing showed the box -- the module's ONLY new capability -- could be deleted
# outright and every test still passed. The single box test that existed had the same expected
# answer with the filter removed, because the in-box contract was also the nearest to target.


def test_box_filter_beats_the_score_when_they_disagree():
    """The box must EXCLUDE, not merely down-weight.

    delta 0.50 is exactly on target and would win on score alone; delta 0.20 is far from it but
    is the only one inside the band. Deleting the filter returns the 0.50 -- which is what every
    previous box test failed to notice.
    """
    cands = [c(100, delta=0.50), c(105, delta=0.20)]
    chosen = pick(cands, ctx(target=0.50, box_min=0.10, box_max=0.25), SelectionPolicy())
    assert chosen.strike == 105.0


@pytest.mark.parametrize("delta", [0.10, 0.25])
def test_box_bounds_are_inclusive(delta):
    chosen = pick([c(100, delta=delta)], ctx(target=0.175, box_min=0.10, box_max=0.25),
                  SelectionPolicy())
    assert chosen is not None


def test_a_box_that_excludes_everything_selects_nothing():
    cands = [c(100, delta=0.60), c(105, delta=0.70)]
    assert pick(cands, ctx(target=0.175, box_min=0.10, box_max=0.25), SelectionPolicy()) is None


def test_an_inverted_box_is_a_configuration_error_not_a_disabled_filter():
    # It used to be silently ignored, handing the GA a search that looked constrained and wasn't.
    with pytest.raises(OptionSelectionConfigError):
        pick([c(100, delta=0.10)], ctx(box_min=0.50, box_max=0.10), SelectionPolicy())


def test_a_one_sided_box_still_filters():
    # box_max alone means "delta at most 0.25". Requiring both bounds discarded it entirely.
    cands = [c(100, delta=0.90), c(105, delta=0.20)]
    assert pick(cands, ctx(target=0.90, box_max=0.25), SelectionPolicy()).strike == 105.0
    cands = [c(100, delta=0.10), c(105, delta=0.80)]
    assert pick(cands, ctx(target=0.10, box_min=0.50), SelectionPolicy()).strike == 105.0


def test_consensus_target_ignores_a_box_rather_than_rejecting_every_contract():
    """A box cannot be expressed in consensus_target's units, because it has no parameter.

    box_value returns None for it, so the fail-closed branch rejected 100% of the chain -- a
    rule that looks configured and silently stops trading.
    """
    cands = [c(105), c(110)]
    chosen = pick(cands, PolicyContext(strike_method="consensus_target", today=TODAY,
                                       target=None, spot=100.0, target_price=107.0,
                                       option_type=OptionRight.CALL,
                                       box_min=0.10, box_max=0.25), SelectionPolicy())
    assert chosen is not None and chosen.strike == 105.0


def test_a_non_finite_value_in_a_ZERO_weighted_field_cannot_change_the_pick():
    """`0.0 * nan` is `nan`, not 0. This is the no-op guarantee's sharpest edge.

    One NaN in iv -- a field the legacy selector never even reads -- turned every score NaN,
    made `min()` compare NaN tuples (all False) and return candidate #0 BY LIST ORDER. Reversing
    the chain changed the answer. `min`/`max` over a list containing NaN are themselves
    order-dependent, which is how the poison spread from one cell to the whole column.
    """
    cands = [c(100, delta=0.31), c(105, delta=0.90, iv=float("nan"))]
    forward = pick(list(cands), ctx(), SelectionPolicy())
    backward = pick(list(reversed(cands)), ctx(), SelectionPolicy())
    assert forward.delta == backward.delta == 0.31


def test_a_non_finite_delta_is_excluded_rather_than_winning():
    # `is not None` let NaN through; it then normalised to 0.0, inverted to a perfect 1.0 and
    # beat a real contract. Unknown must never win.
    cands = [c(110, delta=0.50), c(100, delta=float("nan"))]
    assert pick(cands, ctx(), SelectionPolicy()).strike == 110.0


def test_missing_spread_scores_worst_not_best():
    """`_minimise`'s fail-closed direction had no test; flipping it left the suite green.

    It bites real data: per option_selector._publishes_spread, 2,428,468 cached rows carry no
    quote at all, so spread_pct is None for them. Fail-open would make quoteless contracts the
    PREFERRED ones at any positive w_spread.
    """
    no_quote = OptionContract(symbol="XNQ", underlying="X", option_type=OptionRight.CALL,
                              strike=100.0, expiry=NEAR, bid=None, ask=None, last=1.0,
                              implied_volatility=0.25, delta=0.30, volume=100)
    tight = c(105, bid=1.00, ask=1.02)
    wide = c(110, bid=1.00, ask=2.00)
    m = feature_matrix([no_quote, tight, wide], ctx())
    assert m["spread"] == [0.0, 1.0, 0.0]


def test_a_negative_iv_weight_prefers_cheap_volatility():
    """w_iv is the one SIGNED weight, and nothing tested the negative half end-to-end."""
    cands = [c(100, delta=0.30, iv=0.80), c(105, delta=0.30, iv=0.10)]
    assert pick(cands, ctx(), SelectionPolicy(w_iv=5.0)).strike == 100.0     # rich vol wins
    assert pick(cands, ctx(), SelectionPolicy(w_iv=-5.0)).strike == 105.0    # cheap vol wins


def test_put_deltas_are_compared_on_magnitude():
    """Put deltas are negative; the distance metric must use |delta|. No test used a PUT."""
    def p(strike, delta):
        return OptionContract(symbol=f"P{strike}", underlying="X", option_type=OptionRight.PUT,
                              strike=float(strike), expiry=NEAR, bid=1.0, ask=1.2, last=None,
                              implied_volatility=0.25, delta=delta, volume=100)
    cands = [p(95, -0.20), p(90, -0.45)]
    chosen = pick(cands, PolicyContext(strike_method="delta", today=TODAY, target=0.20,
                                       spot=100.0, option_type=OptionRight.PUT),
                  SelectionPolicy())
    assert chosen.strike == 95.0


def test_percent_otm_box_measures_the_same_side_the_target_aims_at():
    """box_value and target_strike must agree on direction even with option_type unset.

    They used to branch oppositely -- target_strike CALL-first, box_value PUT-first -- so with
    option_type=None a 5% target resolved to 95.0 (the put side) while the box measured strike
    95 as -5% and strike 105 as +5% (the call side). A box of (3, 7) then excluded the very
    contract the target aimed at and admitted its mirror.
    """
    from ba2_common.core.option_selection_policy import box_value
    from ba2_common.core.option_selector import target_strike
    for option_type in (None, OptionRight.CALL, OptionRight.PUT):
        cx = PolicyContext(strike_method="percent_otm", today=TODAY, target=5.0, spot=100.0,
                           option_type=option_type)
        aimed = target_strike("percent_otm", 5.0, 100.0, None, option_type)
        assert box_value(c(aimed), cx) == pytest.approx(5.0), (
            f"option_type={option_type}: target is {aimed} but the box measures it as "
            f"{box_value(c(aimed), cx)}")


def test_is_default_is_false_once_any_gene_is_set():
    assert not SelectionPolicy(w_premium=0.1).is_default
    assert not SelectionPolicy(w_iv=-0.1).is_default
    assert SelectionPolicy().is_default


def test_a_zero_dte_contract_does_not_divide_by_zero_in_the_premium_feature():
    same_day = c(100, expiry=TODAY)
    m = feature_matrix([same_day, c(105)], ctx())
    assert m["premium"][0] == 0.0        # unmeasurable -> worst, not infinite


# --- closing the five mutants that survived the first valid mutation run ---------------------


def test_a_non_finite_value_in_a_WEIGHTED_field_cannot_change_the_pick():
    """The companion to the zero-weighted case, and it tests a DIFFERENT guard.

    Those two fixes mask each other: with zero weights skipped, `iv` is never computed, so the
    zero-weight test never exercises `_normalise`'s non-finite handling at all. Weighting `iv`
    forces the column to be built, which is the only way to reach it. Both guards are needed --
    either alone suffices for the case it covers and neither covers both.
    """
    cands = [c(100, delta=0.31, iv=0.25), c(105, delta=0.90, iv=float("nan"))]
    pol = SelectionPolicy(w_iv=3.0)
    forward = pick(list(cands), ctx(), pol)
    backward = pick(list(reversed(cands)), ctx(), pol)
    assert forward.strike == backward.strike
    # The NaN contract must not win on a feature it cannot answer.
    assert forward.strike == 100.0


def test_when_no_candidate_has_a_finite_delta_nothing_is_selected():
    """The `is not None` filter admitted NaN; only a finiteness check empties this chain.

    With one good delta present the NaN loses anyway (it normalises to the worst score), which
    is why the single-NaN test cannot distinguish the two filters. When EVERY delta is NaN the
    difference is the whole answer: excluded gives None, admitted gives an arbitrary contract
    whose delta nobody can measure.
    """
    cands = [c(100, delta=float("nan")), c(105, delta=float("nan"))]
    assert pick(cands, ctx(), SelectionPolicy()) is None


def test_put_delta_distance_uses_magnitude_not_signed_difference():
    """Kills `abs(c.delta - target)` in place of `abs(abs(c.delta) - abs(target))`.

    A PUT test alone is not enough: at target 0.20 both forms rank the same way. The signed
    form always ADDS the magnitudes for a put, so it preserves the ordering whenever the target
    is nearest the smallest |delta|. Target 0.45 -- nearest the LARGEST |delta| -- is where they
    diverge: correct picks strike 90 (|0.45|-|0.45| = 0), signed picks strike 95.
    """
    def put(strike, delta):
        return OptionContract(symbol=f"P{strike}", underlying="X", option_type=OptionRight.PUT,
                              strike=float(strike), expiry=NEAR, bid=1.0, ask=1.2, last=None,
                              implied_volatility=0.25, delta=delta, volume=100)
    cands = [put(95, -0.20), put(90, -0.45)]
    chosen = pick(cands, PolicyContext(strike_method="delta", today=TODAY, target=0.45,
                                       spot=100.0, option_type=OptionRight.PUT),
                  SelectionPolicy())
    assert chosen.strike == 90.0


def test_feature_matrix_computes_only_what_was_asked_for():
    """`only` is what makes a disabled gene cost nothing, in both senses.

    A gene at weight 0 must not merely contribute 0 to the score -- it must not be COMPUTED,
    because `0.0 * nan` is `nan`. Pinning the mechanism directly, since the behavioural
    consequence is also covered by `_normalise`'s guard and so cannot distinguish the two.
    """
    cands = [c(100), c(105)]
    assert set(feature_matrix(cands, ctx(), only=["box_center"])) == {"box_center"}
    assert set(feature_matrix(cands, ctx())) == set(FEATURE_NAMES)


def test_an_unmeasurable_box_value_excludes_the_contract():
    """`_in_box` fails CLOSED, tested directly because `eligible` makes it unreachable.

    Every route into `_in_box` today has already excluded a contract whose box quantity cannot
    be measured -- the delta method filters non-finite deltas, `percent_otm` without a spot
    raises inside `target_strike`, and `consensus_target` has no box at all. The branch is
    therefore defensive, and defensive code with no test is code that quietly changes meaning.
    """
    no_delta = OptionContract(symbol="XND", underlying="X", option_type=OptionRight.CALL,
                              strike=100.0, expiry=NEAR, bid=1.0, ask=1.2, last=None,
                              implied_volatility=0.25, delta=None, volume=100)
    assert _in_box(no_delta, ctx(box_min=0.10, box_max=0.25)) is False
