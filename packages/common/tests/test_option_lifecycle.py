"""Task 6 acceptance: the PURE option lifecycle decision function.

Every test here builds its inputs literally -- no DB, no broker, no clock. ``as_of``
is frozen to a date that is deliberately NOT today, because the whole point of the
``as_of`` parameter is that this function never reads a clock; a test that drifts
with the calendar has missed it.

The recurring theme is the one the design spec calls out: **unknown is never a
value**. A missing expiry, a missing greek and a missing price each have to come
back as ``LIFECYCLE_UNKNOWN`` with a detail naming the input, never as a hold and
never as a fabricated ``0.0``.
"""
import dataclasses
from datetime import date, datetime, timezone

import pytest

from ba2_common.core.option_lifecycle import (
    COVER_REQUIREMENT_UNMEASURABLE,
    COVERED_CALL_STRATEGY,
    LIFECYCLE_BREAKER,
    LIFECYCLE_CLOSING_REASONS,
    LIFECYCLE_COVER_LOST,
    LIFECYCLE_CREDIT_STOP,
    LIFECYCLE_HOLD,
    LIFECYCLE_PROFIT_CAPTURE,
    LIFECYCLE_TESTED,
    LIFECYCLE_UNKNOWN,
    UNDEFINED_RISK_STRATEGIES,
    LifecycleDecision,
    LifecycleLeg,
    OptionStructure,
    decide,
    structure_metrics,
)
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

# Frozen, and deliberately not "today".
AS_OF = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
FAR_EXPIRY = date(2026, 4, 17)      # 46 DTE from AS_OF
NEAR_EXPIRY = date(2026, 3, 23)     # 21 DTE from AS_OF -- exactly at the default roll window


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def settings(**over):
    """The PremiumSeller defaults, spelled out. Tests override only what they mean."""
    s = {
        "profit_capture_pct": 50.0,
        "strangle_capture_pct": 25.0,
        "tested_delta_enabled": True,
        "tested_delta": 0.30,
        "roll_dte": 21,
        "dr_stop_enabled": True,
        "dr_stop_credit_mult": 2.0,
        "ur_stop_enabled": True,
        "ur_stop_credit_mult": 2.0,
    }
    s.update(over)
    return s


def contract(symbol, *, bid=None, ask=None, last=None, delta=None,
             strike=100.0, right=OptionRight.PUT, expiry=FAR_EXPIRY,
             underlying="XYZ"):
    return OptionContract(symbol=symbol, underlying=underlying, option_type=right,
                          strike=strike, expiry=expiry, bid=bid, ask=ask, last=last,
                          delta=delta)


def put_credit_spread(*, txn_id=1, credit=2.00, qty=1, expiry=FAR_EXPIRY,
                      strategy="put_credit_spread", declared_expiry="same"):
    """Short the 100 put, long the 95 put. Entry net premium = a 2.00 credit."""
    legs = [
        LifecycleLeg("XYZ_P100", net_qty=-1.0 * qty, strike=100.0,
                     option_type=OptionRight.PUT, expiry=expiry, underlying="XYZ"),
        LifecycleLeg("XYZ_P95", net_qty=1.0 * qty, strike=95.0,
                     option_type=OptionRight.PUT, expiry=expiry, underlying="XYZ"),
    ]
    return OptionStructure(
        transaction_id=txn_id, underlying="XYZ", strategy=strategy, legs=legs,
        quantity=qty, multiplier=100, entry_net_premium=-abs(credit),
        expiry=(expiry if declared_expiry == "same" else declared_expiry))


def spread_chain(*, short_ask=1.30, short_bid=1.20, long_bid=0.10, long_ask=0.15,
                 short_delta=0.20, long_delta=0.08):
    """Default: 1.20 to close a 2.00 credit -> +40%. Healthy, and clear of every rail."""
    return {
        "XYZ_P100": contract("XYZ_P100", bid=short_bid, ask=short_ask,
                             delta=-short_delta if short_delta is not None else None,
                             strike=100.0),
        "XYZ_P95": contract("XYZ_P95", bid=long_bid, ask=long_ask,
                            delta=-long_delta if long_delta is not None else None,
                            strike=95.0),
    }


def only(decisions):
    assert len(decisions) == 1, f"expected exactly one decision, got {decisions!r}"
    return decisions[0]


# --------------------------------------------------------------------------
# the five headline exits
# --------------------------------------------------------------------------
def test_a_structure_at_the_profit_target_is_closed():
    """2.00 credit, now costs 0.80 to close -> 60% captured, past the 50% target."""
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=0.90, long_bid=0.10)   # close cost 0.80 -> +60%
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_PROFIT_CAPTURE
    assert d.should_close is True
    assert d.pnl_pct == pytest.approx(60.0)
    assert "50" in d.detail


def test_a_structure_past_the_credit_multiple_stop_is_closed():
    """2.00 credit, now costs 6.50 to close -> -225%, past the 2x-credit stop (-200%)."""
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=6.60, long_bid=0.10)   # close cost 6.50 -> -225%
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_CREDIT_STOP
    assert d.should_close is True
    assert d.pnl_pct == pytest.approx(-225.0)


def test_a_single_expiry_structure_in_its_roll_window_is_NOT_closed_HERE():
    """18 DTE against roll_dte 21, and this module no longer owns that exit (2026-09-03).

    It used to return ``LIFECYCLE_ROLL_DTE`` and the live pass closed it -- an exit the
    backtest could only reproduce if the strategy's ruleset happened to carry ``opt_dte``,
    so live exited more aggressively than any grid result could show. The rule owns it in
    both runtimes now, so the decider says HOLD: nothing IT owns fired. The DTE is still
    measured and still reported in the detail, because a hold that cannot say how much life
    is left is the blindness this module exists to refuse.
    """
    st = put_credit_spread(expiry=date(2026, 3, 20))      # 18 DTE
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.reason == LIFECYCLE_HOLD
    assert d.should_close is False
    assert "18 DTE" in d.detail
    # Every decision still carries the P&L at the moment it was taken.
    assert d.pnl_pct == pytest.approx(40.0)


def test_a_tested_short_is_defended():
    """The short put's |delta| reached 0.42, past the 0.30 tested threshold."""
    st = put_credit_spread()
    chain = spread_chain(short_delta=0.42)
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_TESTED
    assert d.should_close is True
    assert "XYZ_P100" in d.detail
    assert d.pnl_pct == pytest.approx(40.0)


def test_a_healthy_structure_is_left_alone():
    st = put_credit_spread()
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.reason == LIFECYCLE_HOLD
    assert d.should_close is False
    assert d.pnl_pct == pytest.approx(40.0)   # 2.00 credit, 1.20 to close


# --------------------------------------------------------------------------
# unknown is loud
# --------------------------------------------------------------------------
def test_a_structure_with_no_expiry_is_reported_not_silently_held():
    """The old code read parent.expiry, which was NULL for every multi-leg, so the roll
    never fired and nobody knew. Unknown must be loud."""
    st = put_credit_spread(expiry=None, declared_expiry=None)
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.reason == LIFECYCLE_UNKNOWN
    assert d.reason != LIFECYCLE_HOLD
    assert d.should_close is False
    assert "expiry" in d.detail


def test_a_missing_greek_does_not_read_as_a_safe_delta():
    """Unknown is not a value. A tested-delta check with no delta must not conclude 'untested'."""
    st = put_credit_spread()
    chain = spread_chain(short_delta=None)
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_UNKNOWN
    assert d.reason != LIFECYCLE_HOLD
    assert "delta" in d.detail and "XYZ_P100" in d.detail


def test_a_short_leg_absent_from_the_chain_is_unknown_not_untested():
    """One missing row blinds TWO rules -- the P&L and the tested-delta check -- and
    the detail has to name both. Reporting only the P&L would leave a reader thinking
    the tested check had answered, which is the same silent hold in a smaller place."""
    st = put_credit_spread()
    chain = spread_chain()
    del chain["XYZ_P100"]
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_UNKNOWN
    assert "P&L is unmeasurable" in d.detail
    assert "|delta| is unknown" in d.detail
    assert d.detail.count("XYZ_P100") == 2


def test_an_unmeasurable_pnl_is_reported_as_none_never_as_zero():
    """A None P&L that becomes 0.0 reads as 'flat and fine'. It is neither."""
    st = put_credit_spread()
    chain = spread_chain()
    chain["XYZ_P100"] = contract("XYZ_P100", bid=None, ask=None, last=None, delta=-0.2)
    d = only(decide([st], chain, settings(), AS_OF))
    # `is None`, not `== 0` and not falsy-checked: the distinction is the whole point.
    assert d.pnl_pct is None
    assert d.reason == LIFECYCLE_UNKNOWN
    assert "XYZ_P100" in d.detail


def test_an_entry_premium_of_zero_leaves_the_percent_basis_undefined():
    st = put_credit_spread(credit=0.0)
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.pnl_pct is None
    assert d.reason == LIFECYCLE_UNKNOWN
    assert "premium" in d.detail


def test_an_unknown_entry_premium_is_unknown_not_free():
    st = dataclasses.replace(put_credit_spread(), entry_net_premium=None)
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.pnl_pct is None
    assert d.reason == LIFECYCLE_UNKNOWN
    assert "premium" in d.detail


def test_a_zero_contract_multiplier_is_unknown_not_a_division_by_zero():
    st = dataclasses.replace(put_credit_spread(), multiplier=0)
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.pnl_pct is None
    assert d.reason == LIFECYCLE_UNKNOWN
    assert "multiplier" in d.detail


def test_a_zero_structure_quantity_is_unknown_not_a_division_by_zero():
    st = dataclasses.replace(put_credit_spread(), quantity=0.0)
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.pnl_pct is None
    assert d.reason == LIFECYCLE_UNKNOWN
    assert "quantity" in d.detail


def test_a_structure_with_no_held_legs_is_unknown_not_healthy():
    st = OptionStructure(transaction_id=7, underlying="XYZ", strategy="put_credit_spread",
                         legs=[], quantity=1, multiplier=100, entry_net_premium=-2.0,
                         expiry=FAR_EXPIRY)
    d = only(decide([st], {}, settings(), AS_OF))
    assert d.reason == LIFECYCLE_UNKNOWN
    assert "no held" in d.detail


def test_unknown_is_never_folded_into_hold():
    """LIFECYCLE_UNKNOWN and LIFECYCLE_HOLD are different facts and different strings."""
    assert LIFECYCLE_UNKNOWN != LIFECYCLE_HOLD
    assert LIFECYCLE_UNKNOWN not in LIFECYCLE_CLOSING_REASONS
    assert LIFECYCLE_HOLD not in LIFECYCLE_CLOSING_REASONS
    unknown = LifecycleDecision(1, LIFECYCLE_UNKNOWN, "why")
    hold = LifecycleDecision(1, LIFECYCLE_HOLD, "why")
    assert unknown.should_close is False and hold.should_close is False
    assert unknown != hold


def test_every_closing_reason_reports_should_close():
    for reason in (LIFECYCLE_PROFIT_CAPTURE, LIFECYCLE_CREDIT_STOP,
                   LIFECYCLE_TESTED, LIFECYCLE_BREAKER):
        assert LifecycleDecision(1, reason, "d").should_close is True
    for reason in (LIFECYCLE_HOLD, LIFECYCLE_UNKNOWN):
        assert LifecycleDecision(1, reason, "d").should_close is False


# --------------------------------------------------------------------------
# the expiry the parent never had
# --------------------------------------------------------------------------
def test_the_expiry_comes_from_the_legs_when_the_parent_row_has_none():
    """This is the dead roll-DTE gene: parent.expiry was NULL for every multi-leg.
    The legs always knew. Use them."""
    st = put_credit_spread(expiry=date(2026, 3, 20), declared_expiry=None)
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    # HOLD, not UNKNOWN: the legs supplied the expiry, so the DTE was MEASURED (and is in
    # the detail). An unmeasurable one would have been reported as UNKNOWN instead.
    assert d.reason == LIFECYCLE_HOLD
    assert "18 DTE" in d.detail


def test_conflicting_leg_expiries_are_unknown_not_a_guess():
    """submit_option_order refuses multi-expiry structures (Task 2). If one turns up
    anyway, its DTE is undefined -- not max(), not min()."""
    legs = [
        LifecycleLeg("A", net_qty=-1.0, strike=100.0, option_type=OptionRight.PUT,
                     expiry=date(2026, 3, 20)),
        LifecycleLeg("B", net_qty=1.0, strike=95.0, option_type=OptionRight.PUT,
                     expiry=date(2026, 5, 15)),
    ]
    st = OptionStructure(transaction_id=3, underlying="XYZ", strategy="put_credit_spread",
                         legs=legs, quantity=1, multiplier=100, entry_net_premium=-2.0)
    chain = {"A": contract("A", bid=1.2, ask=1.3, delta=-0.2),
             "B": contract("B", bid=0.1, ask=0.15, delta=-0.08)}
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_UNKNOWN
    assert "expir" in d.detail


def test_a_leg_without_an_expiry_does_not_veto_the_one_that_has_it():
    """A single-expiry structure is guaranteed by Task 2; a blank leg adds no conflict."""
    legs = [
        LifecycleLeg("A", net_qty=-1.0, strike=100.0, option_type=OptionRight.PUT,
                     expiry=date(2026, 3, 20)),
        LifecycleLeg("B", net_qty=1.0, strike=95.0, option_type=OptionRight.PUT,
                     expiry=None),
    ]
    st = OptionStructure(transaction_id=3, underlying="XYZ", strategy="put_credit_spread",
                         legs=legs, quantity=1, multiplier=100, entry_net_premium=-2.0)
    chain = {"A": contract("A", bid=1.2, ask=1.3, delta=-0.2),
             "B": contract("B", bid=0.1, ask=0.15, delta=-0.08)}
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_HOLD
    assert "18 DTE" in d.detail       # measured, not vetoed by the blank leg


# --------------------------------------------------------------------------
# thresholds: boundary vs strict
# --------------------------------------------------------------------------
def test_the_profit_target_is_inclusive_at_the_boundary():
    """Exactly 50% captured closes -- '>=', not '>'."""
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=1.10, long_bid=0.10)   # 1.00 to close -> +50.0%
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.pnl_pct == pytest.approx(50.0)
    assert d.reason == LIFECYCLE_PROFIT_CAPTURE


def test_a_hair_below_the_profit_target_is_a_hold():
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=1.11, long_bid=0.10)   # 1.01 to close -> +49.5%
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.pnl_pct == pytest.approx(49.5)
    assert d.reason == LIFECYCLE_HOLD


def test_the_credit_stop_is_inclusive_at_the_boundary():
    """Exactly -200% closes -- '<=', not '<'."""
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=6.10, long_bid=0.10)   # 6.00 to close -> -200.0%
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.pnl_pct == pytest.approx(-200.0)
    assert d.reason == LIFECYCLE_CREDIT_STOP


def test_a_hair_short_of_the_credit_stop_is_a_hold():
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=6.08, long_bid=0.10)   # 5.98 to close -> -199%
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.pnl_pct == pytest.approx(-199.0)
    assert d.reason == LIFECYCLE_HOLD


def test_the_roll_window_boundary_closes_nothing_here_any_more():
    """Exactly ``roll_dte``, the case that used to close on '<=' rather than '<'.

    The boundary itself did not move -- it moved HOUSE. ``days_to_expiry <= N`` in the
    ``opt_dte`` rule is where a single-expiry structure's expiry exit is decided now, in
    both runtimes; ``pmcc_roll_due`` keeps the same '<=' for the overlay roll. What this
    module must not do is close it a second time on a threshold nothing searched.
    """
    st = put_credit_spread(expiry=NEAR_EXPIRY)            # 21 DTE, roll_dte 21
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.reason == LIFECYCLE_HOLD
    assert d.should_close is False


def test_one_day_outside_the_roll_window_is_a_hold():
    st = put_credit_spread(expiry=date(2026, 3, 24))      # 22 DTE
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.reason == LIFECYCLE_HOLD


def test_an_already_expired_structure_is_measured_as_negative_never_clamped():
    """Past expiry is a real (and alarming) state, and the sign carries it: ``-3``, not 0.

    Clamping to 0 would make ``days_to_expiry > 0`` answer "still alive" for a structure
    that expired three days ago -- and the ``opt_dte`` rule that now owns the exit reads the
    same sign convention.
    """
    st = put_credit_spread(expiry=date(2026, 2, 27))      # -3 DTE
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.reason == LIFECYCLE_HOLD
    assert "-3 DTE" in d.detail


def test_the_tested_delta_threshold_is_inclusive_at_the_boundary():
    st = put_credit_spread()
    d = only(decide([st], spread_chain(short_delta=0.30), settings(), AS_OF))
    assert d.reason == LIFECYCLE_TESTED


def test_a_delta_a_hair_below_the_threshold_is_untested():
    st = put_credit_spread()
    d = only(decide([st], spread_chain(short_delta=0.2999), settings(), AS_OF))
    assert d.reason == LIFECYCLE_HOLD


def test_the_tested_check_uses_the_absolute_delta():
    """A short put's delta is negative. Comparing the raw value would never test."""
    st = put_credit_spread()
    chain = spread_chain()
    chain["XYZ_P100"] = contract("XYZ_P100", bid=1.20, ask=1.30, delta=-0.55, strike=100.0)
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_TESTED


def test_only_a_net_short_leg_can_be_tested():
    """A deep long leg is not a tested short. Testing the long side would close
    every healthy spread on day one."""
    st = put_credit_spread()
    chain = spread_chain(short_delta=0.10, long_delta=0.95)
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_HOLD


def test_a_missing_delta_on_a_long_leg_is_not_unknown():
    """We never ask the long leg for a greek, so its absence cannot make us blind."""
    st = put_credit_spread()
    chain = spread_chain(long_delta=None)
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_HOLD


# --------------------------------------------------------------------------
# sign, direction and the wrong-way stop
# --------------------------------------------------------------------------
def test_a_profitable_structure_never_trips_the_credit_stop():
    """A sign error here liquidates winners. -100*mult is a LOSS threshold."""
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=0.02, long_bid=0.01)   # +99.5%
    d = only(decide([st], chain, settings(profit_capture_pct=1000.0), AS_OF))
    assert d.pnl_pct > 0
    assert d.reason == LIFECYCLE_HOLD


def test_a_losing_structure_never_trips_the_profit_target():
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=3.10, long_bid=0.10)   # -50%
    d = only(decide([st], chain, settings(dr_stop_enabled=False), AS_OF))
    assert d.pnl_pct == pytest.approx(-50.0)
    assert d.reason == LIFECYCLE_HOLD


def test_a_long_leg_marks_at_the_bid_and_a_short_leg_at_the_ask():
    """Flattening sells the long (bid) and buys back the short (ask). Swapping them
    flatters every position by the width of the spread."""
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_bid=0.50, short_ask=1.50, long_bid=0.20, long_ask=0.60)
    d = only(decide([st], chain, settings(profit_capture_pct=1000.0), AS_OF))
    # cost to close = 1.50 (buy short back) - 0.20 (sell long) = 1.30 -> +35%
    assert d.pnl_pct == pytest.approx(35.0)


def test_last_is_used_only_when_that_side_of_the_quote_is_missing():
    st = put_credit_spread(credit=2.00)
    chain = {
        "XYZ_P100": contract("XYZ_P100", bid=0.5, ask=None, last=0.90, delta=-0.2),
        "XYZ_P95": contract("XYZ_P95", bid=None, ask=0.9, last=0.10, delta=-0.08),
    }
    d = only(decide([st], chain, settings(profit_capture_pct=1000.0), AS_OF))
    assert d.pnl_pct == pytest.approx(60.0)   # 0.90 - 0.10 = 0.80 to close


def test_cash_already_banked_on_a_closed_leg_counts_toward_the_pnl():
    """A structure whose short wing was bought back for 0.40 has REALISED that cost;
    pricing only the legs still held would report it as a bigger winner than it is."""
    legs = [LifecycleLeg("XYZ_P100", net_qty=0.0, strike=100.0,
                         option_type=OptionRight.PUT, expiry=FAR_EXPIRY),
            LifecycleLeg("XYZ_P95", net_qty=1.0, strike=95.0,
                         option_type=OptionRight.PUT, expiry=FAR_EXPIRY)]
    st = OptionStructure(transaction_id=1, underlying="XYZ", strategy="put_credit_spread",
                         legs=legs, quantity=1, multiplier=100, entry_net_premium=-2.00,
                         realized_cash=-0.40, expiry=FAR_EXPIRY)
    chain = {"XYZ_P95": contract("XYZ_P95", bid=0.05, ask=0.10, delta=-0.02, strike=95.0)}
    d = only(decide([st], chain, settings(profit_capture_pct=1000.0), AS_OF))
    assert d.pnl_pct == pytest.approx(82.5)   # (2.00 - 0.40 + 0.05) / 2.00


# --------------------------------------------------------------------------
# which target / which stop
# --------------------------------------------------------------------------
def test_a_strangle_uses_the_strangle_capture_target():
    """25% for a strangle, not the 50% every other structure gets."""
    legs = [LifecycleLeg("XYZ_P90", net_qty=-1.0, strike=90.0, option_type=OptionRight.PUT,
                         expiry=FAR_EXPIRY),
            LifecycleLeg("XYZ_C110", net_qty=-1.0, strike=110.0, option_type=OptionRight.CALL,
                         expiry=FAR_EXPIRY)]
    st = OptionStructure(transaction_id=1, underlying="XYZ", strategy="short_strangle",
                         legs=legs, quantity=1, multiplier=100, entry_net_premium=-4.0,
                         expiry=FAR_EXPIRY)
    chain = {"XYZ_P90": contract("XYZ_P90", bid=1.4, ask=1.5, delta=-0.10, strike=90.0),
             "XYZ_C110": contract("XYZ_C110", bid=1.4, ask=1.5, delta=0.10, strike=110.0,
                                  right=OptionRight.CALL)}
    d = only(decide([st], chain, settings(), AS_OF))     # 3.00 to close on 4.00 -> +25%
    assert d.pnl_pct == pytest.approx(25.0)
    assert d.reason == LIFECYCLE_PROFIT_CAPTURE
    assert "strangle_capture_pct" in d.detail


def test_a_spread_at_the_same_pnl_does_not_hit_the_strangle_target():
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=1.60, long_bid=0.10)   # 1.50 to close -> +25%
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.pnl_pct == pytest.approx(25.0)
    assert d.reason == LIFECYCLE_HOLD


def test_an_undefined_risk_structure_uses_the_undefined_risk_stop_multiple():
    legs = [LifecycleLeg("XYZ_P100", net_qty=-1.0, strike=100.0,
                         option_type=OptionRight.PUT, expiry=FAR_EXPIRY)]
    st = OptionStructure(transaction_id=1, underlying="XYZ", strategy="short_put",
                         legs=legs, quantity=1, multiplier=100, entry_net_premium=-2.0,
                         expiry=FAR_EXPIRY)
    chain = {"XYZ_P100": contract("XYZ_P100", bid=4.9, ask=5.0, delta=-0.2, strike=100.0)}
    # -150%: past a 1.5x UR stop, short of the 2x DR stop.
    d = only(decide([st], chain, settings(ur_stop_credit_mult=1.5,
                                          dr_stop_credit_mult=2.0), AS_OF))
    assert d.pnl_pct == pytest.approx(-150.0)
    assert d.reason == LIFECYCLE_CREDIT_STOP
    assert "ur_stop" in d.detail


def test_a_defined_risk_structure_uses_the_defined_risk_stop_multiple():
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=5.10, long_bid=0.10)   # 5.00 to close -> -150%
    d = only(decide([st], chain, settings(ur_stop_credit_mult=1.0,
                                          dr_stop_credit_mult=1.5), AS_OF))
    assert d.pnl_pct == pytest.approx(-150.0)
    assert d.reason == LIFECYCLE_CREDIT_STOP
    assert "dr_stop" in d.detail


def test_an_undefined_risk_structure_ignores_the_defined_risk_stop_entirely():
    """Preserved from _should_close's if/elif: turning the UR stop off leaves a naked
    structure with NO stop, even with the DR stop enabled. Surprising, but promoted
    verbatim -- pinned here so any change to it is deliberate."""
    legs = [LifecycleLeg("XYZ_P100", net_qty=-1.0, strike=100.0,
                         option_type=OptionRight.PUT, expiry=FAR_EXPIRY)]
    st = OptionStructure(transaction_id=1, underlying="XYZ", strategy="short_put",
                         legs=legs, quantity=1, multiplier=100, entry_net_premium=-2.0,
                         expiry=FAR_EXPIRY)
    chain = {"XYZ_P100": contract("XYZ_P100", bid=9.9, ask=10.0, delta=-0.2, strike=100.0)}
    d = only(decide([st], chain, settings(ur_stop_enabled=False,
                                          dr_stop_enabled=True), AS_OF))
    assert d.pnl_pct == pytest.approx(-400.0)
    assert d.reason == LIFECYCLE_HOLD


def test_the_undefined_risk_strategies_are_the_two_the_settings_name():
    assert UNDEFINED_RISK_STRATEGIES == ("short_put", "short_strangle")


def test_a_disabled_credit_stop_does_not_fire():
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=6.60, long_bid=0.10)   # -225%
    d = only(decide([st], chain, settings(dr_stop_enabled=False), AS_OF))
    assert d.reason == LIFECYCLE_HOLD


def test_a_disabled_tested_check_does_not_fire_and_does_not_need_a_greek():
    """An input we never consult cannot make us blind. With the check off, a chain
    with no deltas at all is still a confident hold."""
    st = put_credit_spread()
    chain = spread_chain(short_delta=None, long_delta=None)
    d = only(decide([st], chain, settings(tested_delta_enabled=False), AS_OF))
    assert d.reason == LIFECYCLE_HOLD


def test_a_disabled_tested_check_really_ignores_a_tested_short():
    st = put_credit_spread()
    d = only(decide([st], spread_chain(short_delta=0.90),
                    settings(tested_delta_enabled=False), AS_OF))
    assert d.reason == LIFECYCLE_HOLD


# --------------------------------------------------------------------------
# precedence -- pinned so a branch reorder is never silent
# --------------------------------------------------------------------------
def test_profit_capture_outranks_the_roll_window():
    """Both close; only the recorded reason differs, and the reason is what the GA
    reads back. A target hit mislabelled as a roll makes the roll gene look
    profitable and the capture gene look inert."""
    st = put_credit_spread(credit=2.00, expiry=date(2026, 3, 20))   # 18 DTE
    chain = spread_chain(short_ask=0.90, long_bid=0.10)             # +60%
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_PROFIT_CAPTURE


def test_profit_capture_outranks_a_tested_short():
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=0.90, long_bid=0.10, short_delta=0.90)
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_PROFIT_CAPTURE


def test_the_credit_stop_outranks_a_tested_short():
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=6.60, long_bid=0.10, short_delta=0.90)
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_CREDIT_STOP


def test_a_tested_short_outranks_the_roll_window():
    st = put_credit_spread(expiry=date(2026, 3, 20))                # 18 DTE
    chain = spread_chain(short_delta=0.90)
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_TESTED


def test_the_circuit_breaker_outranks_every_per_structure_exit():
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=0.90, long_bid=0.10)             # +60%, would capture
    d = only(decide([st], chain, settings(circuit_breaker_tripped=True), AS_OF))
    assert d.reason == LIFECYCLE_BREAKER
    assert d.should_close is True
    assert d.pnl_pct == pytest.approx(60.0)   # priced, even though the book decided


def test_the_circuit_breaker_flattens_every_structure_including_the_unmeasurable_ones():
    healthy = put_credit_spread(txn_id=1)
    blind = OptionStructure(transaction_id=2, underlying="XYZ", strategy="put_credit_spread",
                            legs=[], quantity=1, multiplier=100, entry_net_premium=None)
    out = decide([healthy, blind], spread_chain(),
                 settings(circuit_breaker_tripped=True), AS_OF)
    assert [d.reason for d in out] == [LIFECYCLE_BREAKER, LIFECYCLE_BREAKER]
    # ...and the one it could not price says so, rather than reporting a flat 0.0.
    assert out[0].pnl_pct == pytest.approx(40.0)
    assert out[1].pnl_pct is None


def test_an_untripped_breaker_changes_nothing():
    st = put_credit_spread()
    d = only(decide([st], spread_chain(), settings(circuit_breaker_tripped=False), AS_OF))
    assert d.reason == LIFECYCLE_HOLD


def test_a_definite_close_outranks_an_unmeasurable_input():
    """UNKNOWN exists to replace a silent HOLD, not to veto a decision we CAN make.

    A structure at its capture target still closes with no greek anywhere -- the missing
    delta only leaves the TESTED rule unasked. (The vehicle was the roll-DTE close until
    2026-09-03, when the ``opt_dte`` rule took that exit over; the principle is unchanged
    and is now pinned on profit capture.)
    """
    st = put_credit_spread(credit=2.00)
    chain = spread_chain(short_ask=0.90, long_bid=0.10,             # 0.80 to close -> +60%
                         short_delta=None, long_delta=None)
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_PROFIT_CAPTURE


def test_several_missing_inputs_are_all_named_in_one_detail():
    st = put_credit_spread(expiry=None, declared_expiry=None)
    chain = spread_chain(short_delta=None)
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_UNKNOWN
    assert "delta" in d.detail and "expiry" in d.detail
    # A fixed order (P&L, then greeks, then the calendar): the detail is compared
    # verbatim by the live/backtest parity test, so it cannot wobble.
    assert d.detail.index("delta") < d.detail.index("expiry")


# --------------------------------------------------------------------------
# cover_lost -- the covered call whose shares left overnight (OPT-L1)
# --------------------------------------------------------------------------
# The case neither cover seam can see. The entry seam refuses to WRITE an uncovered
# covered call and the exit seam refuses to SELL shares pledged to one, but a
# broker-side risk-manager stop (submitted as an OCO leg) fills at 3am: the shares are
# gone, no platform code ran, and the short call is naked until somebody looks. This
# rule is the thing that looks.
#
# Its false positive is expensive -- liquidating a healthy structure -- so the whole
# section turns on ONE distinction: a MEASURED shortfall closes; an UNMEASURABLE cover
# never does.
def covered_call(*, txn_id=1, credit=2.00, qty=1, expiry=FAR_EXPIRY,
                 strategy="covered_call"):
    """Short the 110 call against shares held elsewhere. Entry = a 2.00 credit."""
    legs = [LifecycleLeg("XYZ_C110", net_qty=-1.0 * qty, strike=110.0,
                         option_type=OptionRight.CALL, expiry=expiry, underlying="XYZ")]
    return OptionStructure(
        transaction_id=txn_id, underlying="XYZ", strategy=strategy, legs=legs,
        quantity=qty, multiplier=100, entry_net_premium=-abs(credit), expiry=expiry)


def call_chain(*, ask=1.50, bid=1.40, delta=0.20):
    """Default: 1.50 to close a 2.00 credit -> +25%. Healthy, and clear of every rail."""
    return {"XYZ_C110": contract("XYZ_C110", bid=bid, ask=ask, delta=delta, strike=110.0,
                                 right=OptionRight.CALL)}


def test_a_covered_call_whose_shares_are_gone_is_closed():
    """The 3am stop: 100 shares required, the broker now reports 0. NAKED."""
    st = covered_call()
    d = only(decide([st], call_chain(), settings(), AS_OF,
                    cover_shares_required=100, cover_shares_held=0))
    assert d.reason == LIFECYCLE_COVER_LOST
    assert d.should_close is True
    assert "NAKED" in d.detail
    assert "100" in d.detail
    # Priced like every other decision -- the outcome table has to know what the
    # emergency exit banked, not merely that it happened.
    assert d.pnl_pct == pytest.approx(25.0)


def test_a_partially_stripped_cover_is_still_lost():
    """One 100-share lot of two sold away leaves a 2-contract call 100 short. A rule
    that only fired on zero would miss the multi-lot case the codebase models."""
    st = covered_call(qty=2)
    d = only(decide([st], call_chain(), settings(), AS_OF,
                    cover_shares_required=200, cover_shares_held=100))
    assert d.reason == LIFECYCLE_COVER_LOST
    assert "short by 100" in d.detail


def test_a_covered_call_whose_shares_are_still_there_is_left_alone():
    st = covered_call()
    d = only(decide([st], call_chain(), settings(), AS_OF,
                    cover_shares_required=100, cover_shares_held=100))
    assert d.reason != LIFECYCLE_COVER_LOST
    assert d.reason == LIFECYCLE_HOLD
    assert d.should_close is False


def test_exactly_enough_cover_is_covered():
    """`held >= required`, not `>`. A call covered to the share is covered."""
    st = covered_call(qty=3)
    d = only(decide([st], call_chain(), settings(), AS_OF,
                    cover_shares_required=300, cover_shares_held=300))
    assert d.reason == LIFECYCLE_HOLD


def test_one_share_short_is_short():
    st = covered_call(qty=3)
    d = only(decide([st], call_chain(), settings(), AS_OF,
                    cover_shares_required=300, cover_shares_held=299))
    assert d.reason == LIFECYCLE_COVER_LOST


def test_an_unmeasurable_cover_is_never_read_as_a_lost_one():
    """THE rule of this task. `None` is the POSITION FEED failing to answer, not the
    shares leaving. Firing cover_lost on it liquidates a healthy structure over a feed
    hiccup -- a self-inflicted loss, and the exact conflation this project has spent
    five incidents separating. It must alarm, not act."""
    st = covered_call()
    d = only(decide([st], call_chain(), settings(), AS_OF,
                    cover_shares_required=100, cover_shares_held=None))
    assert d.reason != LIFECYCLE_COVER_LOST
    assert d.should_close is False
    # ...and it is NOT a hold either: the unknown marker is set, naming the input.
    assert d.reason == LIFECYCLE_UNKNOWN
    assert d.reason != LIFECYCLE_HOLD
    assert "cover" in d.detail and "UNKNOWN" in d.detail
    assert "XYZ" in d.detail


def test_an_unreadable_cover_requirement_is_unknown_not_uncovered():
    """A requirement we cannot read is a question we cannot answer, in either
    direction: neither 'naked' nor 'fine'."""
    st = covered_call()
    d = only(decide([st], call_chain(), settings(), AS_OF,
                    cover_shares_required="lots", cover_shares_held=0))
    assert d.reason == LIFECYCLE_UNKNOWN
    assert "unreadable" in d.detail


def test_the_unmeasurable_cover_sentinel_reaches_the_alarm_branch():
    """THE SENTINEL IS PART OF THIS MODULE'S INPUT CONTRACT, not a private string the
    caller happens to send.

    ``option_lifecycle_service`` sets a covered_call's requirement to
    ``COVER_REQUIREMENT_UNMEASURABLE`` when the ledger cannot size the obligation. It
    reaches the alarm only because ``_share_count``'s ``float()`` raises — which worked
    by luck for as long as the value was declared at the other end and the contract
    lived implicitly inside an ``except (TypeError, ValueError)``. Stated here instead:
    it is neither of the two things ``decide`` DOES understand (``None`` = this caller
    is not measuring cover here, which skips the rule silently; a number = a fabricated
    obligation), and it lands on the alarm.

    The alarm is UNKNOWN, never a close: an obligation of unknown size is not a
    measured shortfall, and liquidating on it would be the false positive this whole
    section exists to avoid."""
    assert COVER_REQUIREMENT_UNMEASURABLE is not None
    assert not isinstance(COVER_REQUIREMENT_UNMEASURABLE, (int, float))

    st = covered_call()
    d = only(decide([st], call_chain(), settings(), AS_OF,
                    cover_shares_required=COVER_REQUIREMENT_UNMEASURABLE,
                    cover_shares_held=0))
    assert d.reason == LIFECYCLE_UNKNOWN
    assert d.reason != LIFECYCLE_COVER_LOST
    assert d.should_close is False
    assert "unreadable" in d.detail
    assert COVER_REQUIREMENT_UNMEASURABLE in d.detail, (
        "the alarm must name the value it could not read")

    # ...and it is NOT the 'not asked' case, which skips the rule and says nothing.
    silent = only(decide([st], call_chain(), settings(), AS_OF,
                         cover_shares_required=None, cover_shares_held=0))
    assert silent.reason != LIFECYCLE_UNKNOWN


def test_the_service_sends_the_sentinel_this_module_declares():
    """The other half of the pin, and the reason the constant moved here: the SENDER's
    value and the RECEIVER's contract must be ONE fact. The service used to define its
    own copy, so the two lined up by coincidence. Same treatment as
    ``COVERED_CALL_STRATEGY`` above, for the same reason."""
    from ba2_trade_platform.core.option_lifecycle_service import (
        COVER_REQUIREMENT_UNMEASURABLE as SERVICE_SENTINEL,
    )

    assert SERVICE_SENTINEL == COVER_REQUIREMENT_UNMEASURABLE


def test_the_cover_alarm_leads_the_unknown_detail():
    """Several blind inputs at once: the one that can hide a naked short call is the
    sentence the operator has to read first."""
    st = covered_call(expiry=None)
    chain = call_chain(delta=None)
    d = only(decide([st], chain, settings(), AS_OF,
                    cover_shares_required=100, cover_shares_held=None))
    assert d.reason == LIFECYCLE_UNKNOWN
    assert d.detail.index("cover") < d.detail.index("delta") < d.detail.index("expiry")


def test_a_structure_that_is_not_a_covered_call_is_untouched_by_the_cover_rule():
    """Only the covered_call tag promises SHARE cover -- the same line the entry guard
    draws. A short strangle is MEANT to be naked, and closing it on a share count it
    never claimed would be a liquidation with no cause."""
    st = put_credit_spread()
    d = only(decide([st], spread_chain(), settings(), AS_OF,
                    cover_shares_required=100, cover_shares_held=0))
    assert d.reason != LIFECYCLE_COVER_LOST
    assert d.reason == LIFECYCLE_HOLD


def test_the_strategy_tag_is_matched_case_and_space_insensitively():
    st = covered_call(strategy="  Covered_Call ")
    d = only(decide([st], call_chain(), settings(), AS_OF,
                    cover_shares_required=100, cover_shares_held=0))
    assert d.reason == LIFECYCLE_COVER_LOST


def test_a_caller_that_measures_no_cover_is_completely_unaffected():
    """The default. Every pre-existing caller passes neither argument, and a covered
    call must then decide exactly as it did before -- not become permanently UNKNOWN
    because nobody supplied a share count."""
    st = covered_call()
    d = only(decide([st], call_chain(), settings(), AS_OF))
    assert d.reason == LIFECYCLE_HOLD
    assert d.pnl_pct == pytest.approx(25.0)


def test_the_cover_arithmetic_rounds_the_way_the_two_accessors_do():
    """A REQUIREMENT rounds up and a HOLDING rounds down — the directions
    ``shares_pledged_to_short_calls`` and ``held_shares_for_cover`` already use, so the
    dust falls the same way at both ends of the cover ledger. 99.5 against 99.5 is 99
    shares against 100 required: short."""
    st = covered_call()
    d = only(decide([st], call_chain(), settings(), AS_OF,
                    cover_shares_required=99.5, cover_shares_held=99.5))
    assert d.reason == LIFECYCLE_COVER_LOST
    assert "99 XYZ share(s)" in d.detail and "100 required" in d.detail


def test_a_covered_call_needing_no_cover_is_never_lost():
    """The short call has been bought back: nothing can be called away, so no share
    count can make this structure naked. Without the `required <= 0` skip an empty
    position feed would 'close' a flat structure forever."""
    st = covered_call()
    d = only(decide([st], call_chain(), settings(), AS_OF,
                    cover_shares_required=0, cover_shares_held=0))
    assert d.reason == LIFECYCLE_HOLD
    # ...and a cover we cannot measure is not even worth mentioning when none is needed.
    d2 = only(decide([st], call_chain(), settings(), AS_OF,
                     cover_shares_required=0, cover_shares_held=None))
    assert d2.reason == LIFECYCLE_HOLD


def test_cover_lost_outranks_profit_capture():
    """Both close, so only the RECORDED REASON differs -- and the reason is the alarm.
    Filing a naked short call under 'profit_capture' hides the incident inside a
    winner, and the sleeve's attribution then shows a strategy closing winners early
    for no cause anyone can find."""
    st = covered_call(credit=2.00)
    chain = call_chain(ask=0.90, bid=0.80)          # 0.90 to close -> +55%
    plain = only(decide([st], chain, settings(), AS_OF))
    assert plain.reason == LIFECYCLE_PROFIT_CAPTURE  # precondition: it WOULD capture
    d = only(decide([st], chain, settings(), AS_OF,
                    cover_shares_required=100, cover_shares_held=0))
    assert d.reason == LIFECYCLE_COVER_LOST
    assert d.pnl_pct == pytest.approx(55.0)


def test_cover_lost_outranks_the_credit_stop():
    st = covered_call(credit=2.00, expiry=date(2026, 3, 20))     # 18 DTE
    chain = call_chain(ask=6.60, bid=6.50)                        # -230%, would stop
    d = only(decide([st], chain, settings(), AS_OF,
                    cover_shares_required=100, cover_shares_held=0))
    assert d.reason == LIFECYCLE_COVER_LOST


def test_the_circuit_breaker_still_outranks_cover_lost():
    """Both flatten the position; the breaker is the book-level fact and stays first."""
    st = covered_call()
    d = only(decide([st], call_chain(), settings(circuit_breaker_tripped=True), AS_OF,
                    cover_shares_required=100, cover_shares_held=0))
    assert d.reason == LIFECYCLE_BREAKER


def test_a_definite_close_outranks_an_unmeasurable_cover():
    """UNKNOWN replaces a silent hold, never a decision we CAN make: a covered call at its
    capture target still closes while its cover is unreadable.

    (The vehicle was the roll-DTE close until 2026-09-03, when the ``opt_dte`` rule took
    that exit over. Same principle, a rule this module still owns.)"""
    st = covered_call(credit=2.00)
    d = only(decide([st], call_chain(ask=0.90, bid=0.80), settings(), AS_OF,
                    cover_shares_required=100, cover_shares_held=None))
    assert d.reason == LIFECYCLE_PROFIT_CAPTURE


def test_cover_is_supplied_per_transaction_over_a_book():
    """A pass manages several structures at once, so the figures arrive keyed by
    transaction. One naked call must not tar the healthy ones, and a transaction the
    mapping omits is simply not being measured for cover."""
    naked = covered_call(txn_id=1)
    covered = covered_call(txn_id=2)
    unmeasured = covered_call(txn_id=3)
    out = decide([naked, covered, unmeasured], call_chain(), settings(), AS_OF,
                 cover_shares_required={1: 100, 2: 100},
                 cover_shares_held={1: 0, 2: 100})
    assert [d.reason for d in out] == [LIFECYCLE_COVER_LOST, LIFECYCLE_HOLD,
                                       LIFECYCLE_HOLD]


def test_a_transaction_whose_held_count_is_missing_from_the_mapping_is_unknown():
    """An omitted HELD entry is not 'zero shares': the caller has a requirement for
    this structure and could not answer it, which is the unmeasurable case."""
    st = covered_call(txn_id=4)
    d = only(decide([st], call_chain(), settings(), AS_OF,
                    cover_shares_required={4: 100}, cover_shares_held={}))
    assert d.reason == LIFECYCLE_UNKNOWN
    assert d.reason != LIFECYCLE_COVER_LOST


def test_cover_lost_is_a_closing_reason_and_a_distinct_one():
    assert LIFECYCLE_COVER_LOST in LIFECYCLE_CLOSING_REASONS
    assert LIFECYCLE_COVER_LOST == "cover_lost"
    assert LifecycleDecision(1, LIFECYCLE_COVER_LOST, "d").should_close is True
    assert LIFECYCLE_COVER_LOST not in (LIFECYCLE_HOLD, LIFECYCLE_UNKNOWN)


def test_the_covered_call_tag_is_the_one_the_entry_guard_polices():
    """Two halves of one promise: the entry seam refuses to WRITE this tag uncovered,
    this module CLOSES it once the cover leaves. They must agree on the string. The
    interface cannot be imported by the pure module (the leak gate forbids it), so the
    equality is pinned here instead of by an import."""
    from ba2_common.core.interfaces.OptionsAccountInterface import COVERED_CALL_STRATEGY as GUARD_TAG

    assert COVERED_CALL_STRATEGY == GUARD_TAG


# --------------------------------------------------------------------------
# determinism and shape
# --------------------------------------------------------------------------
def test_decisions_come_back_in_transaction_id_order_whatever_the_input_order():
    sts = [put_credit_spread(txn_id=i) for i in (9, 2, 5, 1)]
    out = decide(sts, spread_chain(), settings(), AS_OF)
    assert [d.transaction_id for d in out] == [1, 2, 5, 9]


def test_the_same_inputs_always_produce_the_same_decisions():
    sts = [put_credit_spread(txn_id=i) for i in (3, 1, 2)]
    a = decide(sts, spread_chain(), settings(), AS_OF)
    b = decide(list(reversed(sts)), spread_chain(), settings(), AS_OF)
    assert a == b


def test_the_order_the_caller_built_the_legs_in_changes_nothing():
    """Two blind shorts. Which one the detail names must be a property of the
    structure, not of the order the caller happened to append its legs in."""
    call = LifecycleLeg("XYZ_C110", net_qty=-1.0, strike=110.0,
                        option_type=OptionRight.CALL, expiry=FAR_EXPIRY)
    put = LifecycleLeg("XYZ_P90", net_qty=-1.0, strike=90.0,
                       option_type=OptionRight.PUT, expiry=FAR_EXPIRY)
    chain = {"XYZ_C110": contract("XYZ_C110", bid=1.7, ask=1.8, delta=None, strike=110.0,
                                  right=OptionRight.CALL),
             "XYZ_P90": contract("XYZ_P90", bid=1.7, ask=1.8, delta=None, strike=90.0)}

    def build(legs):
        return OptionStructure(transaction_id=1, underlying="XYZ", strategy="short_strangle",
                               legs=legs, quantity=1, multiplier=100,
                               entry_net_premium=-4.0, expiry=FAR_EXPIRY)

    forwards = only(decide([build([call, put])], chain, settings(), AS_OF))
    backwards = only(decide([build([put, call])], chain, settings(), AS_OF))
    assert forwards == backwards
    assert forwards.reason == LIFECYCLE_UNKNOWN
    assert "XYZ_C110" in forwards.detail


def test_a_negatively_signed_structure_quantity_does_not_flip_the_pnl():
    """TradeConditions takes abs() of the transaction quantity for the percent basis.
    A short structure recorded with a negative count must not report a 60% winner as
    a 60% loser -- which would trip the credit stop on a position that is winning."""
    st = dataclasses.replace(put_credit_spread(credit=2.00), quantity=-1.0)
    chain = spread_chain(short_ask=0.90, long_bid=0.10)
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.pnl_pct == pytest.approx(60.0)
    assert d.reason == LIFECYCLE_PROFIT_CAPTURE


def test_one_decision_per_structure():
    sts = [put_credit_spread(txn_id=1), put_credit_spread(txn_id=2)]
    assert len(decide(sts, spread_chain(), settings(), AS_OF)) == 2


def test_no_structures_is_no_decisions():
    assert decide([], {}, settings(), AS_OF) == []


def test_a_plain_date_as_of_is_accepted():
    st = put_credit_spread(expiry=date(2026, 3, 20))
    d = only(decide([st], spread_chain(), settings(), date(2026, 3, 2)))
    # The DTE is still computed off a bare ``date`` (no datetime coercion needed) and the
    # number reaches the detail — which is all this test was ever about.
    assert "18 DTE" in d.detail


def test_a_missing_required_setting_is_an_error_not_a_default():
    """No silent defaults: a threshold that was never configured must be loud."""
    st = put_credit_spread()
    s = settings()
    del s["roll_dte"]
    with pytest.raises(KeyError, match="roll_dte"):
        decide([st], spread_chain(), s, AS_OF)


def test_the_decision_is_frozen():
    d = LifecycleDecision(1, LIFECYCLE_HOLD, "d")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.reason = LIFECYCLE_BREAKER


# --------------------------------------------------------------------------
# structure_metrics -- the promoted _txn_metrics
# --------------------------------------------------------------------------
def test_a_naked_short_put_is_undefined_risk_at_full_notional():
    legs = [LifecycleLeg("P", net_qty=-2.0, strike=100.0, option_type=OptionRight.PUT)]
    m = structure_metrics(OptionStructure(1, "XYZ", "short_put", legs))
    assert m.is_defined_risk is False
    assert m.notional == pytest.approx(20000.0)
    assert m.committed == pytest.approx(20000.0)


def test_a_put_credit_spread_commits_only_the_wing_width():
    legs = [LifecycleLeg("PS", net_qty=-1.0, strike=100.0, option_type=OptionRight.PUT),
            LifecycleLeg("PL", net_qty=1.0, strike=95.0, option_type=OptionRight.PUT)]
    m = structure_metrics(OptionStructure(1, "XYZ", "put_credit_spread", legs))
    assert m.is_defined_risk is True
    assert m.committed == pytest.approx(500.0)


def test_a_call_credit_spread_is_defined_risk_too():
    """The promoted formula was min(short strikes) - max(long strikes), which is
    NEGATIVE for a call spread (the long sits above the short) and so reported a
    defined-risk structure as naked."""
    legs = [LifecycleLeg("CS", net_qty=-1.0, strike=100.0, option_type=OptionRight.CALL),
            LifecycleLeg("CL", net_qty=1.0, strike=105.0, option_type=OptionRight.CALL)]
    m = structure_metrics(OptionStructure(1, "XYZ", "call_credit_spread", legs))
    assert m.is_defined_risk is True
    assert m.committed == pytest.approx(500.0)


def test_an_iron_condor_is_defined_risk_not_naked():
    """min(short) - max(long) = 90 - 115 = -25 on the old formula -> 'naked', with the
    whole 110-strike notional committed. It is a 500-dollar-per-side condor."""
    legs = [LifecycleLeg("PS", net_qty=-1.0, strike=90.0, option_type=OptionRight.PUT),
            LifecycleLeg("PL", net_qty=1.0, strike=85.0, option_type=OptionRight.PUT),
            LifecycleLeg("CS", net_qty=-1.0, strike=110.0, option_type=OptionRight.CALL),
            LifecycleLeg("CL", net_qty=1.0, strike=115.0, option_type=OptionRight.CALL)]
    m = structure_metrics(OptionStructure(1, "XYZ", "iron_condor", legs))
    assert m.is_defined_risk is True
    assert m.committed == pytest.approx(1000.0)


def test_an_uncovered_extra_short_makes_the_structure_undefined_risk():
    legs = [LifecycleLeg("PS", net_qty=-2.0, strike=100.0, option_type=OptionRight.PUT),
            LifecycleLeg("PL", net_qty=1.0, strike=95.0, option_type=OptionRight.PUT)]
    m = structure_metrics(OptionStructure(1, "XYZ", "ratio_spread", legs))
    assert m.is_defined_risk is False
    assert m.committed == pytest.approx(500.0 + 10000.0)


def test_a_leg_already_bought_back_stops_committing_capital():
    """_txn_metrics bucketed by order SIDE without netting, so a buy-to-close landed in
    `longs` while the original short stayed in `shorts` forever -- committed capital
    could only ever go up. Here the short is flat and only the long wing is held."""
    legs = [LifecycleLeg("PS", net_qty=0.0, strike=100.0, option_type=OptionRight.PUT),
            LifecycleLeg("PL", net_qty=1.0, strike=95.0, option_type=OptionRight.PUT)]
    m = structure_metrics(OptionStructure(1, "XYZ", "put_credit_spread", legs))
    assert m.is_defined_risk is True
    assert m.notional == pytest.approx(0.0)
    assert m.committed == pytest.approx(0.0)


def test_the_notional_is_a_per_side_stress_basis_not_the_sum_of_both_sides():
    """A strangle cannot be breached on both sides at once, which is why _txn_metrics
    stressed the largest short rather than totalling them. Preserved deliberately --
    it is the denominator of the leverage rail, so a change here silently re-sizes
    every book."""
    legs = [LifecycleLeg("PS", net_qty=-1.0, strike=90.0, option_type=OptionRight.PUT),
            LifecycleLeg("CS", net_qty=-1.0, strike=110.0, option_type=OptionRight.CALL)]
    m = structure_metrics(OptionStructure(1, "XYZ", "short_strangle", legs))
    assert m.notional == pytest.approx(11000.0)     # 110 x 100 x 1, not 90+110
    assert m.is_defined_risk is False


def test_a_short_leg_with_no_strike_is_unknown_not_zero_notional():
    """`float(o.strike or 0.0)` turned an unknown strike into zero notional, which
    reads to the leverage rail as free money."""
    legs = [LifecycleLeg("P", net_qty=-1.0, strike=None, option_type=OptionRight.PUT)]
    m = structure_metrics(OptionStructure(1, "XYZ", "short_put", legs))
    assert m.notional is None and m.committed is None
    assert "strike" in m.detail


def test_a_structure_with_no_legs_at_all_is_unknown_not_riskless():
    """(True, 0.0, 0.0) for 'I could not see any legs' is the all-zeros regime that
    made every rail unreachable on live multi-legs before the leg reconciliation."""
    m = structure_metrics(OptionStructure(1, "XYZ", "put_credit_spread", []))
    assert m.is_defined_risk is None
    assert m.notional is None and m.committed is None
    assert m.detail


def test_an_all_long_structure_is_defined_risk_with_no_short_notional():
    legs = [LifecycleLeg("CL", net_qty=1.0, strike=105.0, option_type=OptionRight.CALL)]
    m = structure_metrics(OptionStructure(1, "XYZ", "long_call", legs))
    assert m.is_defined_risk is True
    assert m.notional == pytest.approx(0.0)
    assert m.committed == pytest.approx(0.0)


def test_a_short_leg_with_no_option_type_is_unknown_not_paired():
    legs = [LifecycleLeg("PS", net_qty=-1.0, strike=100.0, option_type=None),
            LifecycleLeg("PL", net_qty=1.0, strike=95.0, option_type=OptionRight.PUT)]
    m = structure_metrics(OptionStructure(1, "XYZ", "put_credit_spread", legs))
    assert m.is_defined_risk is None
    assert "option type" in m.detail


# --------------------------------------------------------------------------
# purity
# --------------------------------------------------------------------------
def test_the_module_reaches_for_no_database_and_no_broker():
    """Pure means pure: importing it must not drag in the ORM, the trade store or
    any account interface. A DB import here is how 'pure' quietly stops being true."""
    from ._leakgate import check_leak

    verdict = check_leak(
        "ba2_common.core.option_lifecycle",
        ["sqlmodel", "sqlalchemy", "ba2_common.core.db", "ba2_common.core.models",
         "ba2_common.core.trade_store", "ba2_common.core.interfaces",
         "ba2_common.core.TradeConditions", "ba2_providers", "ba2_experts",
         "ba2_trade_platform"],
    )
    assert verdict == "CLEAN", f"option_lifecycle is not pure: {verdict}"
