# packages/common/tests/test_option_selector_wing.py
from datetime import date
from ba2_common.core.option_selector import select_wing
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight


def _c(strike, otype=OptionRight.CALL, oi=500, bid=1.0, ask=1.1):
    return OptionContract(
        symbol=f"X{int(strike)}{'C' if otype==OptionRight.CALL else 'P'}",
        underlying="X", option_type=otype, strike=float(strike),
        expiry=date(2024, 6, 21), bid=bid, ask=ask, last=bid, open_interest=oi,
        delta=None, implied_volatility=None)


def test_select_wing_call_picks_higher_strike():
    chain = [_c(s) for s in (100, 105, 110, 115, 120)]
    w = select_wing(chain, center_strike=100.0, width_pct=10.0,
                    option_type=OptionRight.CALL, dte_min=None, dte_max=None,
                    today=date(2024, 6, 1))
    assert w is not None and w.strike == 110.0


def test_select_wing_put_picks_lower_strike():
    chain = [_c(s, OptionRight.PUT) for s in (80, 85, 90, 95, 100)]
    w = select_wing(chain, center_strike=100.0, width_pct=10.0,
                    option_type=OptionRight.PUT, dte_min=None, dte_max=None,
                    today=date(2024, 6, 1))
    assert w is not None and w.strike == 90.0
