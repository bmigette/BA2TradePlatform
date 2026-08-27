"""Max loss for every structure family, cross-checked against closed-form arithmetic.

WHY EACH CASE. These are the structures whose reserve (broker margin) and max loss DIFFER, so
they are precisely the ones a reader might assume the reserve table already answers:

  * cash-secured put   reserve strike*100          max loss (strike-credit)*100
  * jade lizard        reserve (K_p+width-c)*100   max loss (K_p-c)*100  <- reserve OVERSTATES
  * covered call       reserve 0                   max loss (basis-credit)*100
  * short straddle     reserve Reg-T               max loss UNBOUNDED
"""
import pytest

from ba2_common.core.option_payoff import (
    MEASURED, UNBOUNDED, UNMEASURABLE, PayoffLeg, max_loss)
from ba2_common.core.types import OrderDirection


def leg(kind, side, premium, strike=None, ratio=1):
    return PayoffLeg(kind=kind, side=side, premium=premium, strike=strike, ratio=ratio)


LONG, SHORT = OrderDirection.BUY, OrderDirection.SELL


def test_long_call_max_loss_is_the_debit():
    r = max_loss([leg("call", LONG, 5.0, 100)])
    assert r.state == MEASURED and r.amount == pytest.approx(500.0)


def test_credit_vertical_max_loss_is_width_less_credit():
    # Short 100 call for 3.0, long 105 call for 1.0 -> credit 2.0, width 5.
    legs = [leg("call", SHORT, 3.0, 100), leg("call", LONG, 1.0, 105)]
    r = max_loss(legs)
    assert r.state == MEASURED and r.amount == pytest.approx(300.0)


def test_cash_secured_put_max_loss_is_strike_less_credit_not_the_full_strike():
    r = max_loss([leg("put", SHORT, 3.0, 90)])
    assert r.state == MEASURED and r.amount == pytest.approx(8700.0)


def test_iron_condor_with_unequal_wings_uses_the_WIDER_wing():
    # Put side 90/85 (width 5), call side 110/118 (width 8), total credit 3.0.
    legs = [leg("put", SHORT, 2.0, 90), leg("put", LONG, 1.0, 85),
            leg("call", SHORT, 2.5, 110), leg("call", LONG, 0.5, 118)]
    r = max_loss(legs)
    assert r.state == MEASURED and r.amount == pytest.approx(500.0)  # (8 - 3) * 100


def test_jade_lizard_max_loss_is_the_put_side_only():
    # Short 90 put 2.0, short 105 call 1.5, long 110 call 0.5 -> credit 3.0, call width 5.
    # Reserve would be (90 + 5 - 3) * 100 = 9200. True max loss is (90 - 3) * 100 = 8700.
    legs = [leg("put", SHORT, 2.0, 90),
            leg("call", SHORT, 1.5, 105), leg("call", LONG, 0.5, 110)]
    r = max_loss(legs)
    assert r.state == MEASURED and r.amount == pytest.approx(8700.0)


def test_covered_call_max_loss_is_basis_less_credit_NOT_basis_less_strike_less_credit():
    # 100 shares at 100, short 105 call for 2. The strike caps the UPSIDE; the downside runs
    # to zero. Intuition says (100 - 105 - 2); arithmetic says (100 - 2) * 100.
    legs = [PayoffLeg(kind="stock", side=LONG, premium=100.0),
            leg("call", SHORT, 2.0, 105)]
    r = max_loss(legs)
    assert r.state == MEASURED and r.amount == pytest.approx(9800.0)


def test_protective_put_max_loss_is_basis_less_strike_plus_debit():
    legs = [PayoffLeg(kind="stock", side=LONG, premium=100.0),
            leg("put", LONG, 3.0, 95)]
    r = max_loss(legs)
    assert r.state == MEASURED and r.amount == pytest.approx(800.0)


def test_short_straddle_is_unbounded_not_a_large_number():
    legs = [leg("call", SHORT, 4.0, 100), leg("put", SHORT, 3.5, 100)]
    r = max_loss(legs)
    assert r.state == UNBOUNDED
    assert r.amount is None


def test_short_strangle_is_unbounded():
    legs = [leg("call", SHORT, 2.0, 110), leg("put", SHORT, 2.0, 90)]
    assert max_loss(legs).state == UNBOUNDED


def test_put_ratio_spread_is_bounded_because_the_underlying_stops_at_zero():
    # Long one 100 put, short two 90 puts. Net short one put below 90 -> bounded at S=0.
    legs = [leg("put", LONG, 6.0, 100), leg("put", SHORT, 2.5, 90, ratio=2)]
    r = max_loss(legs)
    assert r.state == MEASURED


def test_bad_leg_is_unmeasurable_and_says_why():
    r = max_loss([leg("call", LONG, 5.0, None)])
    assert r.state == UNMEASURABLE and "strike" in r.reason


def test_a_structure_that_cannot_lose_is_unmeasurable_not_free_money():
    # Long 100 call for 1.0 AND short 100 call for 4.0 -> a 3.0 credit for zero risk.
    # That is an arbitrage, i.e. a stale or crossed quote.
    legs = [leg("call", LONG, 1.0, 100), leg("call", SHORT, 4.0, 100)]
    r = max_loss(legs)
    assert r.state == UNMEASURABLE
    assert "arbitrage" in r.reason


def test_measured_amount_is_always_positive():
    r = max_loss([leg("call", LONG, 5.0, 100)])
    assert r.amount > 0
