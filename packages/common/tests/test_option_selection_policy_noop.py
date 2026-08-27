"""THE NO-OP GUARANTEE: at default weights, ``pick`` selects what ``_pick_by`` selects.

WHY THIS IS THE MOST IMPORTANT TEST IN PHASE 1. The selection policy is being introduced into a
path that fourteen live rules and every option backtest already run through. It ships safely only
if turning it on WITHOUT configuring any weight changes nothing at all — not "changes little",
not "changes only in ties". This test is the evidence for that claim, and it must cover the
awkward cases specifically: exact ties, duplicate strikes across two expiries, and contracts with
no delta (which ``_pick_by`` filters out rather than ranking last).
"""
from datetime import date

import pytest

from ba2_common.core.option_selection_policy import PolicyContext, SelectionPolicy, pick
from ba2_common.core.option_selector import _pick_by
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

TODAY = date(2024, 3, 4)
NEAR = date(2024, 4, 5)
FAR = date(2024, 4, 12)


def c(strike, *, delta=0.30, expiry=NEAR, right=OptionRight.CALL, bid=1.0, ask=1.2):
    return OptionContract(
        symbol=f"X{int(strike * 1000):08d}{expiry:%y%m%d}", underlying="X",
        option_type=right, strike=float(strike), expiry=expiry, bid=bid, ask=ask, last=None,
        implied_volatility=0.25, delta=delta, volume=100)


CHAINS = {
    "spread_of_deltas": [c(95, delta=0.62), c(100, delta=0.48), c(105, delta=0.31),
                         c(110, delta=0.19)],
    "exact_tie_on_distance": [c(95, delta=0.20), c(105, delta=0.40)],
    "duplicate_strike_two_expiries": [c(100, delta=0.30, expiry=FAR),
                                      c(100, delta=0.30, expiry=NEAR)],
    "all_identical": [c(100, delta=0.30), c(105, delta=0.30), c(110, delta=0.30)],
    "some_missing_delta": [c(100, delta=None), c(105, delta=0.33), c(110, delta=None)],
    # THE CHAIN THAT CATCHES A DROPPED DELTA FILTER. With only SOME deltas missing, a policy
    # that ranked the unmeasurable ones worst still agrees with ``_pick_by`` — the one
    # measurable contract wins either way — so that chain alone proves nothing. Only when
    # EVERY delta is missing do the two answers part: ``_pick_by`` returns None, while a
    # rank-them-worst policy ties them all and hands back the lowest strike. Verified by
    # mutation: deleting the filter leaves the rest of this suite green.
    "all_missing_delta": [c(100, delta=None), c(105, delta=None)],
    "single_candidate": [c(100, delta=0.30)],
}


@pytest.mark.parametrize("name", sorted(CHAINS))
@pytest.mark.parametrize("target", [0.15, 0.30, 0.50])
def test_delta_method_matches_pick_by_at_default_weights(name, target):
    cands = CHAINS[name]
    legacy = _pick_by("delta", cands, target, 100.0, None, OptionRight.CALL)
    policy = pick(cands,
                  PolicyContext(strike_method="delta", target=target, spot=100.0,
                                option_type=OptionRight.CALL, today=TODAY),
                  SelectionPolicy())
    assert policy is legacy


@pytest.mark.parametrize("name", sorted(CHAINS))
@pytest.mark.parametrize("param", [0.0, 5.0, 12.0])
def test_percent_otm_matches_pick_by_at_default_weights(name, param):
    cands = CHAINS[name]
    legacy = _pick_by("percent_otm", cands, param, 100.0, None, OptionRight.CALL)
    policy = pick(cands,
                  PolicyContext(strike_method="percent_otm", target=param, spot=100.0,
                                option_type=OptionRight.CALL, today=TODAY),
                  SelectionPolicy())
    assert policy is legacy


@pytest.mark.parametrize("name", sorted(CHAINS))
def test_consensus_target_matches_pick_by_at_default_weights(name):
    cands = CHAINS[name]
    legacy = _pick_by("consensus_target", cands, None, 100.0, 107.0, OptionRight.CALL)
    policy = pick(cands,
                  PolicyContext(strike_method="consensus_target", target=None, spot=100.0,
                                target_price=107.0, option_type=OptionRight.CALL, today=TODAY),
                  SelectionPolicy())
    assert policy is legacy


@pytest.mark.parametrize("name", sorted(CHAINS))
def test_consensus_target_with_no_price_matches_pick_by_by_selecting_nothing(name):
    # The reachable no-target case, and the one place the no-op is easiest to lose:
    # ``select_single``'s ``target_price`` defaults to None, so a consensus_target rule on a
    # recommendation that carried no price reaches ``_pick_by`` with nothing to aim at and gets
    # None back (``target_strike`` -> None -> return None). A policy that merely SCORED the
    # unmeasurable distance would tie every candidate and hand back the lowest strike, i.e.
    # open a deep-ITM position where the legacy selector opened none. Both must refuse.
    cands = CHAINS[name]
    legacy = _pick_by("consensus_target", cands, None, 100.0, None, OptionRight.CALL)
    policy = pick(cands,
                  PolicyContext(strike_method="consensus_target", target=None, spot=100.0,
                                target_price=None, option_type=OptionRight.CALL, today=TODAY),
                  SelectionPolicy())
    assert legacy is None
    assert policy is legacy


def test_chain_order_does_not_change_the_default_pick():
    cands = CHAINS["duplicate_strike_two_expiries"]
    ctx = PolicyContext(strike_method="delta", target=0.30, spot=100.0,
                        option_type=OptionRight.CALL, today=TODAY)
    forward = pick(list(cands), ctx, SelectionPolicy())
    backward = pick(list(reversed(cands)), ctx, SelectionPolicy())
    assert forward.expiry == backward.expiry == NEAR
