"""``pick`` applies the box filter, the weights and the tie-break, in that order."""
from datetime import date

import pytest

from ba2_common.core.option_selection_policy import PolicyContext, SelectionPolicy, pick
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


def test_delta_method_with_no_target_selects_nothing_rather_than_guessing():
    # option_selector's docstring says a None strike_param under the delta method is a
    # misconfigured ruleset and _pick_by raises on it. Scoring instead would make every
    # distance unmeasurable, tie every candidate, and quietly hand back the lowest strike --
    # a real contract chosen for no reason. An empty result is a refusal the caller can report.
    assert pick([c(100, delta=0.30)], ctx(target=None), SelectionPolicy()) is None


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
