# packages/common/tests/test_new_option_actions.py
from datetime import date
from types import SimpleNamespace
import pytest

from ba2_common.core.TradeActions import create_action
from ba2_common.core.option_types import OptionContract, OptionLeg
from ba2_common.core.types import ExpertActionType, OptionRight, OrderDirection
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """These tests persist TradeActionResult rows. Sibling DB-seam tests
    (test_db_seam / test_threadlocal_db) repoint the global DB seam at their own
    temp sqlite without restoring it, so by the time this module runs the
    session DB may lack the trade_action_result table. Re-point to a fresh,
    fully-initialized sqlite for each test here so it is order-independent."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "actions.sqlite"))
    db.init_db()
    yield


class FakeAccount(OptionsAccountInterface):
    """Minimal options account capturing submit_option_order calls."""
    def __init__(self, spot=100.0):
        self.id = 1
        self._spot = spot
        self.submitted = []
    # capability + clock
    def _as_of_date(self):
        return date(2024, 6, 1)
    def get_balance(self):
        return 100_000.0
    def get_instrument_current_price(self, symbol, price_type=None):
        return self._spot
    def get_current_price(self, symbol=None):
        return self._spot
    # chain: strikes around spot for both rights, 30 DTE
    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        for s in range(80, 121, 5):
            out.append(OptionContract(
                symbol=f"{underlying}{s}{'C' if option_type==OptionRight.CALL else 'P'}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=date(2024, 6, 21), bid=2.0, ask=2.2, last=2.1,
                open_interest=1000, delta=None, implied_volatility=None))
        return out
    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        order = SimpleNamespace(id=len(self.submitted) + 1, data={})
        self.submitted.append(dict(legs=legs, quantity=quantity,
                                   limit_price=limit_price, strategy=option_strategy))
        return order
    # unused abstract bits
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
        return 100_000.0


def _mk(action_type, **kw):
    acct = FakeAccount()
    rec = SimpleNamespace(id=1, instance_id=None)
    act = create_action(ExpertActionType(action_type), "AAPL", acct,
                        SimpleNamespace(), None, rec, **kw)
    act.submit_to_broker = True
    return acct, act


def test_short_strangle_two_short_legs_credit():
    acct, act = _mk("open_short_strangle", strike_method="percent_otm",
                    strike_param=10.0, dte_min=20, dte_max=40, sizing=20.0)
    res = act.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[0]
    assert sub["strategy"] == "short_strangle"
    assert len(sub["legs"]) == 2
    assert all(l.side == OrderDirection.SELL for l in sub["legs"])
    assert sub["limit_price"] < 0   # credit
    assert sub["quantity"] >= 1


def test_short_straddle_same_strike_both_short():
    acct, act = _mk("open_short_straddle", strike_method="percent_otm",
                    strike_param=0.0, dte_min=20, dte_max=40, sizing=20.0)
    res = act.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[0]
    assert sub["strategy"] == "short_straddle"
    legs = sub["legs"]
    assert len(legs) == 2 and legs[0].strike == legs[1].strike
    assert all(l.side == OrderDirection.SELL for l in legs)
    assert sub["limit_price"] < 0
