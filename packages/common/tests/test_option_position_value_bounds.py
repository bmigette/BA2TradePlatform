"""``option_payoff.position_value_bounds`` -- the structure's true expiry VALUE range, the one
definition a mark-to-market clamp must use (and ``max_loss`` for unbalanced butterflies).

A call butterfly whose UPPER wing is wider than its lower one is worth ``(k2-k1) - (k3-k2)``
above ``k3`` -- NEGATIVE -- so it can lose more than its net debit, and a ``[0, width]`` clamp
floors a real liability at zero.
"""
from __future__ import annotations

import math

import pytest

from ba2_common.core.option_payoff import (
    MEASURED, UNBOUNDED, PayoffLeg, max_loss, position_value_bounds,
)
from ba2_common.core.types import OrderDirection

BUY, SELL = OrderDirection.BUY, OrderDirection.SELL


def _c(strike, side, premium=1.0, ratio=1):
    return PayoffLeg("call", side, premium, strike, ratio)


def _p(strike, side, premium=1.0, ratio=1):
    return PayoffLeg("put", side, premium, strike, ratio)


def test_balanced_butterfly_is_worth_zero_to_its_wing():
    assert position_value_bounds([_c(100, BUY), _c(110, SELL, ratio=2), _c(120, BUY)]) == (0.0, 1000.0)


def test_unbalanced_butterfly_can_be_worth_a_negative_amount():
    lo, hi = position_value_bounds([_c(100, BUY), _c(110, SELL, ratio=2), _c(130, BUY)])
    assert hi == 1000.0
    assert lo == -1000.0                      # (110-100) - (130-110) = -10 a share


def test_butterfly_with_a_narrow_upper_wing_is_unchanged():
    assert position_value_bounds([_c(90, BUY), _c(100, SELL, ratio=2), _c(105, BUY)]) == (0.0, 1000.0)


def test_verticals():
    assert position_value_bounds([_c(100, BUY), _c(110, SELL)]) == (0.0, 1000.0)       # bull call
    assert position_value_bounds([_c(100, SELL), _c(110, BUY)]) == (-1000.0, 0.0)      # bear call
    assert position_value_bounds([_p(100, SELL), _p(90, BUY)]) == (-1000.0, 0.0)       # bull put


def test_iron_condor_with_unequal_wings_is_bounded_by_the_wider_wing():
    ic = [_p(80, BUY), _p(90, SELL), _c(110, SELL), _c(125, BUY)]
    assert position_value_bounds(ic) == (-1500.0, 0.0)


def test_ratio_spread_is_unbounded_below_and_says_so():
    lo, hi = position_value_bounds([_c(100, BUY), _c(110, SELL, ratio=2)])
    assert lo == -math.inf                    # a naked short call: never a finite wrong number
    assert hi == 1000.0


def test_long_call_is_unbounded_above():
    assert position_value_bounds([_c(100, BUY)]) == (0.0, math.inf)


def test_quantity_scales_through_the_leg_ratio():
    lo, hi = position_value_bounds([_c(100, BUY, ratio=3), _c(110, SELL, ratio=6), _c(130, BUY, ratio=3)])
    assert (lo, hi) == (-3000.0, 3000.0)


def test_unusable_legs_raise_instead_of_guessing():
    with pytest.raises(ValueError):
        position_value_bounds([PayoffLeg("call", BUY, 1.0, None)])
    with pytest.raises(ValueError):
        position_value_bounds([])


def test_unbalanced_butterfly_max_loss_exceeds_its_debit():
    """The stamped entry ``max_loss`` is derived from the same legs: debit 2.5 plus the 10 a
    share the fly is worth below zero above its top strike."""
    legs = [_c(100, BUY, 12.0), _c(110, SELL, 5.5, ratio=2), _c(130, BUY, 1.5)]
    r = max_loss(legs)
    assert r.state == MEASURED
    assert r.amount == pytest.approx(1250.0)             # > the 250 debit


def test_balanced_butterfly_max_loss_is_its_debit():
    r = max_loss([_c(100, BUY, 12.0), _c(110, SELL, 5.5, ratio=2), _c(120, BUY, 2.5)])
    assert r.amount == pytest.approx(350.0)


def test_ratio_spread_max_loss_is_explicitly_unbounded():
    assert max_loss([_c(100, BUY, 10.0), _c(110, SELL, 5.0, ratio=2)]).state == UNBOUNDED
