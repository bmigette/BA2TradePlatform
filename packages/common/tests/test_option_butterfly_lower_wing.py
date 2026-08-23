"""The call butterfly's LOWER wing is picked by hand, outside ``option_selector`` (2026-08-23).

``select_wing`` moves a wing FARTHER OTM, which for calls means UP; the butterfly's lower
wing is a call BELOW the body, so ``OpenCallButterflyAction`` open-codes that one pick. Being
the only leg in the file selected outside the shared helpers, it is the one whose rules were
untested and had drifted: its ``min()`` key carried a third component, ``c.expiry``, that
``lower_cands`` had already made constant, so it could never break a tie -- reversing it (the
reviewer's mutation M-G) changed nothing and the whole 1034-test package suite stayed green.

Dead weight in a sort key is not harmless: it reads as a rule that is being enforced. These
tests pin the rules that ARE enforced -- the wing shares the body's expiry, sits strictly
below it, and ties go to the lower strike -- so the key can be trimmed to what it does, and
so the next person who widens the candidate filter finds out what they broke.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import create_action
from ba2_common.core.option_types import OptionContract
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.types import ExpertActionType, OptionRight

AS_OF = date(2024, 6, 1)
NEAR = date(2024, 6, 21)          # 20 DTE
FAR = date(2024, 6, 28)           # 27 DTE — also inside the window, same strikes


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "butterfly.sqlite"))
    db.init_db()
    yield


class _TwoExpiryAccount(OptionsAccountInterface):
    """The shape the real cache has: the SAME strikes listed in more than one in-window
    expiry. Premium is convex in strike (intrinsic + decaying time value) so a long
    butterfly comes out as a net debit, as it must."""

    def __init__(self, spot=100.0, *, far_first=False, thin_strikes=()):
        self.id = 1
        self._spot = spot
        self._far_first = far_first
        self._thin = set(thin_strikes)
        self.submitted = []

    def _as_of_date(self):
        return AS_OF

    def get_balance(self):
        return 1_000_000.0

    def get_instrument_current_price(self, symbol, price_type=None):
        return self._spot

    def get_current_price(self, symbol=None):
        return self._spot

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        expiries = [FAR, NEAR] if self._far_first else [NEAR, FAR]
        out = []
        for expiry in expiries:
            for s in range(80, 121, 5):
                strike = float(s)
                otm = max(strike - self._spot, 0.0)
                intrinsic = max(self._spot - strike, 0.0)
                bid = max(0.2, 5.0 - 0.08 * otm) + intrinsic
                out.append(OptionContract(
                    symbol=f"{underlying}{expiry:%y%m%d}C{int(strike * 1000):08d}",
                    underlying=underlying, option_type=option_type, strike=strike,
                    expiry=expiry, bid=round(bid, 4), ask=round(bid + 0.2, 4),
                    last=round(bid, 4), volume=500,
                    open_interest=5 if strike in self._thin else 1000))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(dict(legs=legs, quantity=quantity, limit_price=limit_price))
        return SimpleNamespace(id=len(self.submitted), data={})

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order

    def get_option_quote(self, contract_symbol):
        return None

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None):
        return None

    def check_option_buying_power(self, required):
        return True

    def available_option_buying_power(self):
        return 1_000_000.0


_REC = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                       expected_profit_percent=None, recommended_action=None)


def _butterfly(acct, *, wing_width_pct=10.0, **kw):
    act = create_action(ExpertActionType.OPEN_CALL_BUTTERFLY, "AAPL", acct,
                        SimpleNamespace(), None, _REC,
                        strike_method="percent_otm", strike_param=0.0,
                        dte_min=10, dte_max=40, sizing=50.0,
                        wing_width_pct=wing_width_pct, **kw)
    act.submit_to_broker = True
    res = act.execute()
    assert res["success"] is True, res["message"]
    legs = acct.submitted[-1]["legs"]
    lower, body, upper = sorted(legs, key=lambda leg: leg.strike)
    return lower, body, upper


def test_the_lower_wing_shares_the_bodys_expiry():
    """THE INVARIANT THAT MADE THE EXPIRY TERM DEAD. A butterfly whose legs sit in different
    expiries is a calendar, not a butterfly, so ``lower_cands`` is filtered to the body's
    expiry before the pick runs — after which every candidate's expiry is identical and no
    ``min()`` key component built from it can order anything."""
    lower, body, upper = _butterfly(_TwoExpiryAccount())
    assert body.expiry == lower.expiry == upper.expiry


def test_the_expiry_pin_is_the_filter_not_the_list_order():
    """Feed the LATER expiry first. If the wing were pinned only by luck of input order the
    legs would split across expiries here."""
    lower, body, upper = _butterfly(_TwoExpiryAccount(far_first=True))
    assert body.expiry == lower.expiry == upper.expiry
    assert body.expiry == NEAR, "the body should still take the earliest in-window expiry"


def test_the_lower_wing_pick_does_not_depend_on_chain_order():
    forward = _butterfly(_TwoExpiryAccount())
    reversed_ = _butterfly(_TwoExpiryAccount(far_first=True))
    assert forward[0].contract_symbol == reversed_[0].contract_symbol


def test_the_lower_wing_is_the_strike_nearest_the_width_target():
    """spot 100 -> body 100; 10% below is 90, which is on the grid."""
    lower, body, _upper = _butterfly(_TwoExpiryAccount(), wing_width_pct=10.0)
    assert body.strike == 100.0
    assert lower.strike == 90.0


def test_an_equidistant_lower_wing_resolves_to_the_LOWER_strike():
    """12.5% below a 100 body is 87.5 — exactly between the 85 and 90 strikes. The tie-break
    is the strike, ascending (the same direction ``option_selector._tie`` uses), so the pick
    is deterministic instead of falling back to whatever order the provider returned."""
    lower, body, _upper = _butterfly(_TwoExpiryAccount(), wing_width_pct=12.5)
    assert body.strike == 100.0
    assert lower.strike == 85.0
    lower_rev, _, _ = _butterfly(_TwoExpiryAccount(far_first=True), wing_width_pct=12.5)
    assert lower_rev.strike == 85.0


def test_the_lower_wing_is_strictly_below_the_body():
    lower, body, upper = _butterfly(_TwoExpiryAccount())
    assert lower.strike < body.strike < upper.strike


def test_the_lower_wing_obeys_the_liquidity_gates_like_every_other_leg():
    """It is selected outside the shared helpers, so its gate application is its own code
    path and needs its own proof: starve the natural pick and the next strike down wins."""
    lower, _body, _upper = _butterfly(_TwoExpiryAccount(thin_strikes=(90.0,)),
                                      min_open_interest=100)
    assert lower.strike == 85.0
