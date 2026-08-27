"""Feature normalisation is what makes contract selection work ACROSS symbols.

THE PROBLEM IT SOLVES. An absolute threshold on premium, volume or spread is meaningless across a
$15 stock and a $900 stock — the same "$2.00 of premium" is rich on one and negligible on the
other. Every feature here is therefore min-max normalised WITHIN the candidate set, i.e. it
measures a contract's RANK among its own peers on the chain in front of you, which is scale-free.

FAIL-CLOSED. A candidate missing a feature scores worst on it (0.0), never best — the same
direction as ``option_selector.passes_liquidity``, which refuses a contract whose liquidity is
unknown while its peers report theirs.
"""
from datetime import date

import pytest

from ba2_common.core.option_selection_policy import PolicyContext, SelectionPolicy, feature_matrix
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

TODAY = date(2024, 3, 4)
EXPIRY = date(2024, 4, 5)


def c(strike, *, delta=0.30, bid=1.0, ask=1.2, iv=0.25, volume=100, expiry=EXPIRY):
    return OptionContract(
        symbol=f"X{int(strike * 1000):08d}", underlying="X", option_type=OptionRight.CALL,
        strike=float(strike), expiry=expiry, bid=bid, ask=ask, last=None,
        implied_volatility=iv, delta=delta, volume=volume)


def ctx(**kw):
    base = dict(strike_method="delta", target=0.30, spot=100.0, option_type=OptionRight.CALL,
                today=TODAY)
    base.update(kw)
    return PolicyContext(**base)


def test_box_center_feature_is_highest_for_the_closest_candidate():
    cands = [c(100, delta=0.30), c(105, delta=0.40), c(110, delta=0.50)]
    m = feature_matrix(cands, ctx())
    assert m["box_center"][0] > m["box_center"][1] > m["box_center"][2]


def test_rvol_feature_is_highest_for_the_busiest_contract():
    cands = [c(100, volume=10), c(105, volume=500)]
    m = feature_matrix(cands, ctx())
    assert m["rvol"][1] > m["rvol"][0]


def test_spread_feature_is_highest_for_the_tightest_quote():
    tight = c(100, bid=1.00, ask=1.02)
    wide = c(105, bid=1.00, ask=2.00)
    m = feature_matrix([tight, wide], ctx())
    assert m["spread"][0] > m["spread"][1]


def test_iv_feature_is_highest_for_the_most_expensive_vol():
    m = feature_matrix([c(100, iv=0.20), c(105, iv=0.60)], ctx())
    assert m["iv"][1] > m["iv"][0]


def test_missing_value_scores_worst_not_best():
    # Three candidates, not two: with only one PRESENT value the range is degenerate and every
    # feature legitimately flattens to 0.0, which would let this test pass for the wrong reason.
    m = feature_matrix([c(100, volume=None), c(105, volume=10), c(110, volume=500)], ctx())
    assert m["rvol"][0] == 0.0     # missing -> worst
    assert m["rvol"][1] == 0.0     # lowest present -> also 0.0, but by measurement
    assert m["rvol"][2] == 1.0     # highest present -> best


def test_all_equal_values_contribute_nothing_rather_than_dividing_by_zero():
    m = feature_matrix([c(100, volume=7), c(105, volume=7)], ctx())
    assert m["rvol"] == [0.0, 0.0]


def test_single_candidate_does_not_crash():
    m = feature_matrix([c(100)], ctx())
    assert set(m) == {"box_center", "premium", "iv", "rvol", "spread"}
    assert all(len(v) == 1 for v in m.values())


def test_default_policy_weights_only_the_box_center():
    p = SelectionPolicy()
    assert p.w_box_center == 1.0
    assert (p.w_premium, p.w_iv, p.w_rvol, p.w_spread) == (0.0, 0.0, 0.0, 0.0)
    assert p.is_default
