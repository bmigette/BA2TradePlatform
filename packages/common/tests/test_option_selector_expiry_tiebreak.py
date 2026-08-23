"""Selection must not depend on the ORDER of the chain list (2026-08-23).

WHY: ``select_single``'s sort key was ``(abs(strike - target), strike)`` — no expiry term.
The historical cache lists the SAME strike in more than one in-window expiry (measured on
2024-03-04, 25-45 DTE: BAC 37/39, INTC 42/47, AAPL 49/51), so those candidates tie on BOTH
key components and ``min()`` fell back to input-list order: simply reversing the chain
flipped ``BAC240405C00037000`` -> ``BAC240412C00037000``. Every structure that pins its
remaining legs to a first leg's expiry (iron condor wings, jade lizard wing, butterfly
wings, ratio short, straddle put) inherited that arbitrariness.

Fix: expiry is the FINAL tie-break (nearest strike still wins first, then lowest strike as
before, then the EARLIEST expiry) — it only orders pairs that were previously unordered, so
no pre-existing pick changes.
"""
from datetime import date

import pytest

from ba2_common.core.option_selector import select_single, select_wing
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

TODAY = date(2024, 3, 4)
NEAR = date(2024, 4, 5)
FAR = date(2024, 4, 12)


def _c(strike, expiry, *, right=OptionRight.CALL, delta=0.30, oi=1000, volume=500):
    return OptionContract(
        symbol=f"X{int(strike*1000):08d}{expiry:%y%m%d}", underlying="X",
        option_type=right, strike=float(strike), expiry=expiry,
        bid=1.20, ask=1.30, last=1.25, delta=delta, open_interest=oi, volume=volume)


def test_select_single_percent_otm_same_strike_two_expiries_is_order_independent():
    a = _c(37.0, NEAR)
    b = _c(37.0, FAR)
    pick_fwd = select_single([a, b], method="percent_otm", strike_param=3.0, spot=35.9,
                             option_type=OptionRight.CALL, dte_min=25, dte_max=45, today=TODAY)
    pick_rev = select_single([b, a], method="percent_otm", strike_param=3.0, spot=35.9,
                             option_type=OptionRight.CALL, dte_min=25, dte_max=45, today=TODAY)
    assert pick_fwd.symbol == pick_rev.symbol
    assert pick_fwd.expiry == NEAR          # earliest expiry wins the tie


def test_select_single_delta_same_strike_two_expiries_is_order_independent():
    a = _c(37.0, NEAR, delta=0.30)
    b = _c(37.0, FAR, delta=0.30)
    fwd = select_single([a, b], method="delta", strike_param=0.30, spot=35.9,
                        option_type=OptionRight.CALL, dte_min=25, dte_max=45, today=TODAY)
    rev = select_single([b, a], method="delta", strike_param=0.30, spot=35.9,
                        option_type=OptionRight.CALL, dte_min=25, dte_max=45, today=TODAY)
    assert fwd.symbol == rev.symbol == a.symbol


def test_select_wing_same_strike_two_expiries_is_order_independent():
    a = _c(40.0, NEAR)
    b = _c(40.0, FAR)
    fwd = select_wing([a, b], center_strike=36.0, width_pct=10.0, option_type=OptionRight.CALL,
                      dte_min=25, dte_max=45, today=TODAY)
    rev = select_wing([b, a], center_strike=36.0, width_pct=10.0, option_type=OptionRight.CALL,
                      dte_min=25, dte_max=45, today=TODAY)
    assert fwd.symbol == rev.symbol == a.symbol


def test_strike_distance_still_dominates_expiry():
    """The tie-break must be LAST: a nearer strike in a LATER expiry still wins."""
    near_far_strike = _c(50.0, NEAR)
    far_near_strike = _c(37.0, FAR)
    pick = select_single([near_far_strike, far_near_strike], method="percent_otm",
                         strike_param=3.0, spot=35.9, option_type=OptionRight.CALL,
                         dte_min=25, dte_max=45, today=TODAY)
    assert pick is far_near_strike


def test_lower_strike_still_wins_before_expiry():
    """Pre-existing rule: equal distance -> lower strike, regardless of expiry."""
    lower_far = _c(34.0, FAR)
    upper_near = _c(38.0, NEAR)
    pick = select_single([upper_near, lower_far], method="percent_otm", strike_param=0.0,
                         spot=36.0, option_type=OptionRight.CALL,
                         dte_min=25, dte_max=45, today=TODAY)
    assert pick is lower_far
