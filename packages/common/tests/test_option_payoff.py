"""Payoff at expiry, checked against hand-computed values for real structures.

WHY HAND-COMPUTED AND NOT PROPERTY-ONLY. The whole point of deriving max loss from the payoff
curve is that a hand-written per-structure table drifts. That argument only holds if the curve
itself is right, so the curve is pinned to arithmetic a reader can verify in their head.
"""
import pytest

from ba2_common.core.option_payoff import (
    MEASURED,
    UNBOUNDED,
    UNMEASURABLE,
    PayoffLeg,
    max_profit,
    payoff_at,
    validate_legs,
)
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


def test_a_long_call_has_unbounded_profit_and_is_not_called_unprofitable():
    """THE GUARD ORDER. A long call's payoff is NON-POSITIVE across [0, K_max] -- it is
    the debit, everywhere below the strike. A 'cannot profit anywhere' test running first
    would report every ordinary long call as UNMEASURABLE, the exact mirror of the bug
    max_loss's own ordering comment describes."""
    result = max_profit([long_call(100.0, 2.50)])
    assert result.state == UNBOUNDED
    assert result.reason is None


def test_a_credit_vertical_profits_at_most_its_credit():
    """Short 100c @ 3.00, long 105c @ 1.00 -> net credit 2.00/share = $200/unit."""
    legs = [short_call(100.0, 3.00), long_call(105.0, 1.00)]
    result = max_profit(legs)
    assert result.state == MEASURED
    assert result.amount == pytest.approx(200.0)


def test_a_naked_short_put_profits_at_most_its_credit():
    """Bounded ABOVE (the credit) while unbounded BELOW -- so max_profit is MEASURED on
    the very structure whose max_loss is UNBOUNDED. The two answers are independent."""
    result = max_profit([short_put(90.0, 4.00)])
    assert result.state == MEASURED
    assert result.amount == pytest.approx(400.0)


def test_a_debit_spread_bought_above_its_width_cannot_profit():
    """Long 100c @ 6.00, short 105c @ 1.00 = 5.00 debit for a 5.00-wide spread. Best
    outcome is exactly break-even, which is a crossed or stale quote rather than a trade."""
    legs = [long_call(100.0, 6.00), short_call(105.0, 1.00)]
    result = max_profit(legs)
    assert result.state == UNMEASURABLE
    assert "profit" in result.reason.lower()
