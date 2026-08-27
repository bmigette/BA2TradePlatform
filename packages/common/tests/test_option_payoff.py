"""Payoff at expiry, checked against hand-computed values for real structures.

WHY HAND-COMPUTED AND NOT PROPERTY-ONLY. The whole point of deriving max loss from the payoff
curve is that a hand-written per-structure table drifts. That argument only holds if the curve
itself is right, so the curve is pinned to arithmetic a reader can verify in their head.
"""
import pytest

from ba2_common.core.option_payoff import PayoffLeg, payoff_at, validate_legs
from ba2_common.core.types import OrderDirection


def long_call(strike, premium, ratio=1):
    return PayoffLeg(kind="call", side=OrderDirection.BUY, premium=premium,
                     strike=strike, ratio=ratio)


def short_call(strike, premium, ratio=1):
    return PayoffLeg(kind="call", side=OrderDirection.SELL, premium=premium,
                     strike=strike, ratio=ratio)


def long_put(strike, premium, ratio=1):
    return PayoffLeg(kind="put", side=OrderDirection.BUY, premium=premium,
                     strike=strike, ratio=ratio)


def short_put(strike, premium, ratio=1):
    return PayoffLeg(kind="put", side=OrderDirection.SELL, premium=premium,
                     strike=strike, ratio=ratio)


def long_stock(entry):
    return PayoffLeg(kind="stock", side=OrderDirection.BUY, premium=entry, strike=None)


def test_long_call_below_strike_loses_exactly_the_debit():
    legs = [long_call(100, 5.0)]
    assert payoff_at(legs, 90.0) == pytest.approx(-500.0)


def test_long_call_above_breakeven_is_intrinsic_less_debit():
    legs = [long_call(100, 5.0)]
    assert payoff_at(legs, 110.0) == pytest.approx(500.0)


def test_short_put_at_zero_loses_strike_less_credit():
    legs = [short_put(100, 3.0)]
    assert payoff_at(legs, 0.0) == pytest.approx(-9700.0)


def test_short_put_expiring_worthless_keeps_the_credit():
    legs = [short_put(100, 3.0)]
    assert payoff_at(legs, 120.0) == pytest.approx(300.0)


def test_covered_call_is_capped_above_the_strike():
    # 100 shares bought at 100, short 105 call for 2. Above 105 the payoff is flat at
    # (105 - 100 + 2) * 100 = 700.
    legs = [long_stock(100.0), short_call(105, 2.0)]
    assert payoff_at(legs, 105.0) == pytest.approx(700.0)
    assert payoff_at(legs, 130.0) == pytest.approx(700.0)


def test_stock_leg_defaults_to_the_hundred_shares_backing_one_contract():
    legs = [long_stock(50.0)]
    assert payoff_at(legs, 51.0) == pytest.approx(100.0)


def test_ratio_multiplies_the_leg():
    one = payoff_at([short_put(100, 3.0)], 0.0)
    two = payoff_at([short_put(100, 3.0, ratio=2)], 0.0)
    assert two == pytest.approx(2 * one)


@pytest.mark.parametrize("legs, fragment", [
    ([], "no legs"),
    ([PayoffLeg(kind="future", side=OrderDirection.BUY, premium=1.0, strike=100)],
     "unknown leg kind"),
    ([PayoffLeg(kind="call", side=OrderDirection.BUY, premium=-1.0, strike=100)],
     "not a usable price"),
    ([PayoffLeg(kind="call", side=OrderDirection.BUY, premium=1.0, strike=None)],
     "not a usable strike"),
    ([PayoffLeg(kind="call", side=OrderDirection.BUY, premium=1.0, strike=100, ratio=0)],
     "must be positive"),
    ([PayoffLeg(kind="call", side=OrderDirection.BUY, premium=float("nan"), strike=100)],
     "not a usable price"),
])
def test_validate_legs_names_the_problem(legs, fragment):
    problem = validate_legs(legs)
    assert problem is not None and fragment in problem


def test_validate_legs_accepts_a_good_structure():
    assert validate_legs([long_call(100, 5.0), short_call(110, 2.0)]) is None
