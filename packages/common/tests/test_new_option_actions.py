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
        # Premium = intrinsic value + time value. Time value decays with OTM
        # distance from spot so farther-OTM wings are cheaper than the shorts
        # (required for a defined-risk net credit, e.g. iron condor); ATM stays
        # richest in time value (short straddle credit). Intrinsic value
        # (max(spot-strike,0) for calls, max(strike-spot,0) for puts) is added
        # for ITM contracts. This makes the premium curve CONVEX in strike, which
        # is what makes a long butterfly cost a small net debit
        # (lower.ask + upper.ask > 2*body.bid). Without intrinsic the model is a
        # symmetric V around spot and a butterfly comes out as a credit, which is
        # unrealistic. The OTM-only credit structures are unaffected because none
        # of their legs are ITM (intrinsic == 0 there).
        out = []
        for s in range(80, 121, 5):
            if option_type == OptionRight.CALL:
                otm_dist = max(float(s) - self._spot, 0.0)
                intrinsic = max(self._spot - float(s), 0.0)
            else:
                otm_dist = max(self._spot - float(s), 0.0)
                intrinsic = max(float(s) - self._spot, 0.0)
            bid = max(0.2, 5.0 - 0.08 * otm_dist) + intrinsic
            ask = round(bid + 0.2, 4)
            out.append(OptionContract(
                symbol=f"{underlying}{s}{'C' if option_type==OptionRight.CALL else 'P'}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=date(2024, 6, 21), bid=round(bid, 4), ask=ask, last=round(bid, 4),
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


def test_iron_condor_four_legs_credit_defined_risk():
    acct, act = _mk("open_iron_condor", strike_method="percent_otm",
                    strike_param=10.0, dte_min=20, dte_max=40, sizing=20.0,
                    wing_width_pct=5.0)
    res = act.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[0]
    assert sub["strategy"] == "iron_condor"
    legs = sub["legs"]
    assert len(legs) == 4
    sells = [l for l in legs if l.side == OrderDirection.SELL]
    buys = [l for l in legs if l.side == OrderDirection.BUY]
    assert len(sells) == 2 and len(buys) == 2
    assert sub["limit_price"] < 0  # net credit


def test_jade_lizard_three_legs_credit():
    acct, act = _mk("open_jade_lizard", strike_method="percent_otm",
                    strike_param=10.0, dte_min=20, dte_max=40, sizing=20.0,
                    wing_width_pct=5.0)
    res = act.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[0]
    assert sub["strategy"] == "jade_lizard"
    legs = sub["legs"]
    assert len(legs) == 3
    assert sum(1 for l in legs if l.side == OrderDirection.SELL) == 2
    assert sum(1 for l in legs if l.side == OrderDirection.BUY) == 1
    assert sub["limit_price"] < 0


def test_call_butterfly_three_strikes_ratio_121_debit():
    acct, act = _mk("open_call_butterfly", strike_method="percent_otm",
                    strike_param=0.0, dte_min=20, dte_max=40, sizing=10.0,
                    wing_width_pct=10.0)
    res = act.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[0]
    assert sub["strategy"] == "call_butterfly"
    legs = sub["legs"]
    assert len(legs) == 3
    body = [l for l in legs if l.side == OrderDirection.SELL]
    wings = [l for l in legs if l.side == OrderDirection.BUY]
    assert len(body) == 1 and body[0].ratio_qty == 2
    assert len(wings) == 2 and all(w.ratio_qty == 1 for w in wings)
    assert sub["limit_price"] > 0  # net debit


def test_put_ratio_spread_buy1_sell2():
    acct, act = _mk("open_put_ratio_spread", strike_method="percent_otm",
                    strike_param=5.0, dte_min=20, dte_max=40, sizing=20.0,
                    wing_width_pct=5.0)
    res = act.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[0]
    assert sub["strategy"] == "put_ratio_spread"
    legs = sub["legs"]
    buys = [l for l in legs if l.side == OrderDirection.BUY]
    sells = [l for l in legs if l.side == OrderDirection.SELL]
    assert len(buys) == 1 and buys[0].ratio_qty == 1
    assert len(sells) == 1 and sells[0].ratio_qty == 2
    assert buys[0].strike > sells[0].strike
