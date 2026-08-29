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
    MEASURED, MIN_MEASURABLE_LOSS, UNBOUNDED, UNMEASURABLE, PayoffLeg, critical_points,
    max_loss, payoff_at, upside_slope)
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
    # That is an arbitrage: the same strike cannot be worth 1.0 to buy and 4.0 to sell, so
    # the leg set or the premium signs are wrong.
    legs = [leg("call", LONG, 1.0, 100), leg("call", SHORT, 4.0, 100)]
    r = max_loss(legs)
    assert r.state == UNMEASURABLE
    assert "arbitrage" in r.reason
    # AND IT IS NOT DESCRIBED AS A NEAR-BREAK-EVEN QUOTE. The worst outcome here is +300 at
    # every price; the message used to tell the reader it was "within 0.01 of break-even ...
    # a stale or crossed quote", which is a claim about freshness for a structure whose real
    # fault is that its legs cannot have been built the way they were priced. Two causes, two
    # remedies -- and the counterpart of the assertion in the credit-equals-width test below,
    # which pins the branch that DOES deserve the stale-quote reading.
    assert "stale" not in r.reason and "crossed" not in r.reason
    # "300", not "300.0000": pinning the :.4f specifier would fail on a later formatting
    # change while catching no real defect. This still fails if the branch stops reporting
    # the actual worst case, which is the behaviour under test.
    assert "300" in r.reason
    assert "profits at every underlying price" in r.reason.lower()


def test_measured_amount_is_always_positive():
    r = max_loss([leg("call", LONG, 5.0, 100)])
    assert r.amount > 0


# --- regression tests added after the 2026-08-27 code-quality review -------------------------
#
# The twelve tests above read well and were all green while FOUR single-line mutations survived
# the whole suite, including two on the guard that decides UNBOUNDED. What follows exists to
# constrain the guard itself, not just the structures that happen to exercise it.

def test_call_ratio_spread_is_unbounded():
    """The mutant-killer for ``* leg.ratio`` in ``upside_slope``.

    NO test above puts a ratio != 1 on a CALL or STOCK leg — the put ratio spread uses ratio=2
    on PUTS, which contribute nothing to the upside slope. Drop the ``* leg.ratio`` factor and
    this 1x2 computes slope ``+100 - 100 = 0``, is judged bounded, and returns a finite MEASURED
    figure for a structure that loses $488,200 at an underlying of 5,000. That is the single
    most dangerous verdict this module can emit.
    """
    legs = [leg("call", LONG, 6.0, 100), leg("call", SHORT, 2.0, 110, ratio=2)]
    assert max_loss(legs).state == UNBOUNDED


def test_one_share_against_a_full_contract_short_call_is_unbounded():
    """The mutant-killer for ``* leg.multiplier`` in ``upside_slope``.

    NO test above sets a multiplier at all, so the term is unconstrained. One SHARE of stock
    against a 100-share short call is net short ~99 deltas: true slope -99. Drop the multiplier
    factor and the slope reads ``+1 - 1 = 0``, i.e. covered — the classic naked-call-mistaken-
    for-covered-call error, in arithmetic.
    """
    legs = [PayoffLeg(kind="stock", side=LONG, premium=100.0, multiplier=1.0),
            leg("call", SHORT, 2.0, 105)]
    assert max_loss(legs).state == UNBOUNDED


def test_credit_equal_to_width_is_unmeasurable_not_a_sub_cent_budget():
    """A true max loss of zero must not survive as floating-point dust.

    Short 95 call @ 0.60, long 95.5 call @ 0.10: credit 0.50 == width 0.50, so max loss is
    exactly zero. In IEEE arithmetic the trough lands at -1.78e-15, which a bare ``worst >= 0``
    admits as MEASURED. Sizing is ``floor(budget / max_loss)``, so that is 562,949,953,421,312,000
    contracts on a $1,000 budget.

    The premiums matter: 6.15/1.15 for the same width happens to land on exact zero and passes
    even without the fix, which is how this went unnoticed.
    """
    r = max_loss([leg("call", SHORT, 0.60, 95.0), leg("call", LONG, 0.10, 95.5)])
    assert r.state == UNMEASURABLE
    assert r.amount is None
    # This one genuinely IS within a cent of break-even, so it genuinely IS the stale-quote
    # reading. Paired with the arbitrage test above so that neither branch can be deleted or
    # merged back into one message without a failure.
    assert "stale or crossed" in r.reason


def test_no_measured_loss_is_ever_small_enough_to_size_absurdly():
    """The general form of the case above, swept rather than sampled.

    The sweep also pins the UPPER edge of the break-even branch. 132 of these pairs land
    with a worst case in the dust band ``(0, MIN_MEASURABLE_LOSS]`` -- positive, but by
    around 1e-15 -- and those must keep the break-even reading. Mutating that boundary to
    ``worst <= 0.0`` survived the whole suite before this assertion existed: every one of
    them would move to "structure PROFITS at every underlying price ... a risk-free
    arbitrage of this size does not survive in a live chain", announcing an arbitrage of
    half a cent. That is the exact defect the profit side was corrected for, mirrored.
    """
    checked_dust_band = 0
    for width in (0.5, 1.0, 2.5, 5.0):
        for cents in range(1, 400):
            short_premium = round(cents * 0.01, 2)
            long_premium = round(short_premium - width, 2)
            if long_premium < 0:
                continue
            legs = [leg("call", SHORT, short_premium, 100.0),
                    leg("call", LONG, long_premium, 100.0 + width)]
            r = max_loss(legs)
            if r.state == MEASURED:
                assert r.amount >= MIN_MEASURABLE_LOSS, (
                    f"short {short_premium} / long {long_premium} width {width} produced a "
                    f"MEASURED max loss of {r.amount!r}, which sizes "
                    f"{int(1000 / r.amount):,} contracts on a $1,000 budget")
            elif 0.0 < min(payoff_at(legs, s) for s in critical_points(legs)) \
                    <= MIN_MEASURABLE_LOSS:
                checked_dust_band += 1
                assert "stale or crossed" in r.reason, (
                    f"short {short_premium} / long {long_premium} width {width} has a worst "
                    f"case inside the dust band but was diagnosed as an arbitrage: {r.reason}")

    # The band must not be empty, or the assertion above is vacuous and the boundary is
    # unpinned again without anything failing.
    assert checked_dust_band > 0


def test_empty_legs_reaches_unmeasurable_through_max_loss():
    # validate_legs([]) is tested directly elsewhere; this pins the path THROUGH max_loss.
    r = max_loss([])
    assert r.state == UNMEASURABLE and "no legs" in r.reason


def test_critical_points_are_zero_and_every_strike():
    legs = [leg("call", SHORT, 2.0, 110), leg("put", LONG, 1.0, 85),
            PayoffLeg(kind="stock", side=LONG, premium=100.0)]
    assert critical_points(legs) == [0.0, 85.0, 110.0]


def test_critical_points_come_back_sorted_ascending():
    """``critical_points`` is public, documented, and its ordering had no test that could fail.

    THE STRIKES HERE ARE CHOSEN, NOT ARBITRARY. Replacing ``sorted(points)`` with
    ``list(points)`` survived the whole suite, and an obvious-looking test with strikes 85/110
    STILL could not catch it: CPython iterates a small set of floats in hash order, and
    hash(0.0)=0, hash(85.0)=85, hash(110.0)=110 fall into ascending slots, so the unsorted
    result was accidentally sorted anyway. Strikes 1/5/7/37 iterate as [0, 1, 37, 5, 7], which
    is the distinction the assertion needs.

    Ordering does not affect ``max_loss`` -- ``min()`` does not care -- but ``critical_points``
    is public API whose whole purpose is to hand a caller the kinks of a curve, and handing
    them out of order is a lie about the contract.
    """
    legs = [leg("call", LONG, 0.5, 1.0), leg("call", SHORT, 0.4, 5.0),
            leg("call", LONG, 0.3, 7.0), leg("call", SHORT, 0.2, 37.0)]
    points = critical_points(legs)
    assert points == sorted(points)
    assert points == [0.0, 1.0, 5.0, 7.0, 37.0]


@pytest.mark.parametrize("legs, expected", [
    ([leg("call", LONG, 5.0, 100)], 100.0),
    ([leg("call", SHORT, 5.0, 100)], -100.0),
    ([leg("call", SHORT, 5.0, 100, ratio=3)], -300.0),
    ([leg("put", SHORT, 5.0, 100)], 0.0),                       # puts are worthless up there
    ([leg("put", LONG, 5.0, 100)], 0.0),
    ([PayoffLeg(kind="stock", side=LONG, premium=100.0)], 100.0),
    ([PayoffLeg(kind="stock", side=SHORT, premium=100.0)], -100.0),
    ([PayoffLeg(kind="stock", side=LONG, premium=100.0, multiplier=1.0)], 1.0),
])
def test_upside_slope_is_the_net_call_and_stock_delta(legs, expected):
    """``upside_slope`` decides UNBOUNDED and had no direct test of its own."""
    assert upside_slope(legs) == pytest.approx(expected)


def test_a_lone_short_stock_leg_is_unbounded():
    # The downside is bounded by S=0 for every structure, but a short stock leg runs away
    # UPWARD, which is the direction upside_slope covers.
    assert max_loss([PayoffLeg(kind="stock", side=SHORT, premium=100.0)]).state == UNBOUNDED


def test_a_flat_upside_is_bounded_not_unbounded():
    # Slope exactly 0 must NOT be UNBOUNDED: a covered call sits here, and so does short stock
    # plus a long call. Mutating `< -eps` to `<= 0` would refuse both.
    legs = [PayoffLeg(kind="stock", side=SHORT, premium=100.0), leg("call", LONG, 2.0, 105)]
    r = max_loss(legs)
    assert r.state == MEASURED


def test_bool_multiplier_is_refused_not_read_as_one_share():
    # True is an int subclass and math.isfinite(True) is True, so an unguarded multiplier read
    # it as a 1-share contract and reported a 100x-understated max loss as a MEASUREMENT.
    r = max_loss([PayoffLeg(kind="call", side=LONG, premium=5.0, strike=100, multiplier=True)])
    assert r.state == UNMEASURABLE and "multiplier" in r.reason


@pytest.mark.parametrize("bad_ratio", [float("nan"), float("inf")])
def test_non_finite_ratio_is_refused_not_reported_as_a_nan_loss(bad_ratio):
    # nan <= 0 is False, so an unguarded ratio passed validation, made every payoff nan, and
    # reported MEASURED with amount=nan -- "unknown reads as a number" via the front door.
    r = max_loss([leg("call", LONG, 5.0, 100, ratio=bad_ratio)])
    assert r.state == UNMEASURABLE and "ratio" in r.reason


@pytest.mark.parametrize("field, kwargs", [
    ("premium", dict(kind="call", side=LONG, premium="5.0", strike=100)),
    ("strike", dict(kind="call", side=LONG, premium=5.0, strike="100")),
    ("multiplier", dict(kind="call", side=LONG, premium=5.0, strike=100, multiplier="100")),
])
def test_a_stringly_typed_field_is_returned_as_a_reason_not_raised(field, kwargs):
    """``validate_legs`` promises RETURNED, NOT RAISED -- for any input, not just plausible input.

    It used to call ``math.isfinite`` directly, which raises TypeError on a str. A validator
    that raises is not a partial validator; it fails in precisely the manner it was written to
    prevent, and it is the only guard in front of max_loss.
    """
    r = max_loss([PayoffLeg(**kwargs)])
    assert r.state == UNMEASURABLE and field in r.reason


def test_a_put_priced_above_its_own_strike_is_refused():
    # A put's greatest possible value at expiry is its strike (at an underlying of zero), so a
    # premium above that is a crossed or mis-signed quote. This is the only spot-free quote
    # sanity check available -- and for a short-call structure, where the arbitrage guard never
    # runs, it is the only one there is.
    r = max_loss([leg("put", SHORT, 120.0, 100.0)])
    assert r.state == UNMEASURABLE and "strike" in r.reason


def test_payoff_at_raises_on_an_unknown_kind_rather_than_pricing_it_as_stock():
    # The final branch used to be a catch-all `else`, so a "future" leg was silently priced as
    # stock and returned a plausible 11900.0 while the docstring promised a raise.
    with pytest.raises(ValueError):
        payoff_at([PayoffLeg(kind="future", side=LONG, premium=1.0, strike=100)], 120.0)


def test_a_deep_itm_CALL_may_be_worth_more_than_its_strike():
    """The put-only quote bound must not touch calls. Regression for a real defect.

    The bound exists because a put's greatest possible value at expiry is its strike. A CALL has
    no such ceiling -- its value is unbounded above -- and the comment beside the check says so.
    The check nonetheless sat under `kind in _OPTION_KINDS`, so a strike-50 call marked at 102
    (entirely normal with spot around 160) was refused as a bad quote, max_loss came back
    UNMEASURABLE, and the message accused it of being a put.
    """
    r = max_loss([leg("call", LONG, 102.0, 50.0)])
    assert r.state == MEASURED, f"deep-ITM call refused: {r.reason}"
    assert r.amount == pytest.approx(10_200.0)      # the debit, and nothing else


def test_the_put_bound_still_bites():
    r = max_loss([leg("put", LONG, 102.0, 50.0)])
    assert r.state == UNMEASURABLE and "strike" in r.reason
