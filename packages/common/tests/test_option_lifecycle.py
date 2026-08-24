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
    LIFECYCLE_BREAKER,
    LIFECYCLE_CLOSING_REASONS,
    LIFECYCLE_CREDIT_STOP,
    LIFECYCLE_HOLD,
    LIFECYCLE_PROFIT_CAPTURE,
    LIFECYCLE_ROLL_DTE,
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


def test_a_structure_inside_the_roll_dte_window_is_closed():
    """18 DTE against roll_dte 21. Healthy P&L, healthy delta -- the calendar alone closes it."""
    st = put_credit_spread(expiry=date(2026, 3, 20))      # 18 DTE
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.reason == LIFECYCLE_ROLL_DTE
    assert d.should_close is True
    assert d.detail == "18 DTE <= roll_dte 21"
    # Every decision carries the P&L at the moment it was taken; the outcome table
    # needs to know what a roll actually banked, not just that it happened.
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
    for reason in (LIFECYCLE_PROFIT_CAPTURE, LIFECYCLE_CREDIT_STOP, LIFECYCLE_ROLL_DTE,
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
    assert d.reason == LIFECYCLE_ROLL_DTE
    assert d.detail == "18 DTE <= roll_dte 21"


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
    assert d.reason == LIFECYCLE_ROLL_DTE


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


def test_the_roll_window_is_inclusive_at_the_boundary():
    """Exactly roll_dte closes -- '<=', not '<'."""
    st = put_credit_spread(expiry=NEAR_EXPIRY)            # 21 DTE, roll_dte 21
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.reason == LIFECYCLE_ROLL_DTE
    assert d.detail == "21 DTE <= roll_dte 21"


def test_one_day_outside_the_roll_window_is_a_hold():
    st = put_credit_spread(expiry=date(2026, 3, 24))      # 22 DTE
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.reason == LIFECYCLE_HOLD


def test_an_already_expired_structure_is_inside_the_roll_window():
    st = put_credit_spread(expiry=date(2026, 2, 27))      # -3 DTE
    d = only(decide([st], spread_chain(), settings(), AS_OF))
    assert d.reason == LIFECYCLE_ROLL_DTE
    assert d.detail == "-3 DTE <= roll_dte 21"


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
    18 DTE with no greek anywhere still rolls."""
    st = put_credit_spread(expiry=date(2026, 3, 20))                # 18 DTE
    chain = spread_chain(short_delta=None, long_delta=None)
    d = only(decide([st], chain, settings(), AS_OF))
    assert d.reason == LIFECYCLE_ROLL_DTE


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
    assert d.detail == "18 DTE <= roll_dte 21"


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
