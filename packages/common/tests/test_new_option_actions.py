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


# --------------------------------------------------------------------------- #
# Evaluator wiring: the REAL backtest/live path runs option actions through
# TradeActionEvaluator, NOT create_action directly. Three hardcoded action-type
# lists in the evaluator (the execute() routing list, the _create_trade_action
# option branch, and the _get_action_type_from_action class map) silently DROPPED
# the 6 new action types — so they never submitted, producing zero fills (-1e9
# sentinel) in the optimizer even though the action classes themselves work.
# These tests lock the wiring so a new option action can never be added to the
# action classes without also being routed by the evaluator.
# --------------------------------------------------------------------------- #
_NEW_OPTION_ACTION_CLASSES = {
    "OpenShortStraddleAction": ExpertActionType.OPEN_SHORT_STRADDLE,
    "OpenShortStrangleAction": ExpertActionType.OPEN_SHORT_STRANGLE,
    "OpenIronCondorAction": ExpertActionType.OPEN_IRON_CONDOR,
    "OpenJadeLizardAction": ExpertActionType.OPEN_JADE_LIZARD,
    "OpenCallButterflyAction": ExpertActionType.OPEN_CALL_BUTTERFLY,
    "OpenPutRatioSpreadAction": ExpertActionType.OPEN_PUT_RATIO_SPREAD,
}


@pytest.mark.parametrize("class_name,expected_type", list(_NEW_OPTION_ACTION_CLASSES.items()))
def test_evaluator_maps_new_option_action_classes(class_name, expected_type):
    """_get_action_type_from_action must resolve every new option action class to its
    ExpertActionType. If it returns None, execute() routes the action to the
    'unknown type' branch and silently drops it (the root cause of the zero-fill bug)."""
    import ba2_common.core.TradeActions as TA
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

    action_cls = getattr(TA, class_name)
    inst = action_cls.__new__(action_cls)  # don't need a fully-constructed action
    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    resolved = ev._get_action_type_from_action(inst)
    assert resolved == expected_type, (
        f"{class_name} resolved to {resolved}, expected {expected_type}; the evaluator "
        f"would drop this action as 'unknown type' and it would never submit/fill"
    )


def test_evaluator_option_type_lists_cover_all_option_actions():
    """Every option ExpertActionType in the canonical get_option_action_values() must be
    routed by the evaluator's execute() order-creating list (so the action submits)."""
    from ba2_common.core.types import get_option_action_values
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_ACTION_TYPES

    canonical = {ExpertActionType(v) for v in get_option_action_values()}
    # CLOSE_OPTION is routed too but is not an "entry"; entry list = canonical - {CLOSE_OPTION}
    expected_entries = canonical - {ExpertActionType.CLOSE_OPTION}
    assert expected_entries.issubset(_OPTION_ENTRY_ACTION_TYPES), (
        f"missing from evaluator entry list: {expected_entries - _OPTION_ENTRY_ACTION_TYPES}"
    )


def test_evaluator_forwards_wing_width_pct():
    """_create_trade_action must forward wing_width_pct to the option action ctor so
    iron condor / jade lizard / butterfly / ratio-spread get their configured wing."""
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS
    assert "wing_width_pct" in _OPTION_ENTRY_PARAM_KEYS


# --------------------------------------------------------------------------- #
# Naked short-premium reserve = Reg-T margin, NOT full strike*100 cash. The old
# full-cash proxy ($22k for one AAPL contract) made short straddle/strangle, jade
# lizard, and put ratio spread impossible to size on a realistic account, so they
# never opened. The margin model (~20% of notional) reserves what a broker actually
# does for a naked short, making the structures sizeable.
# --------------------------------------------------------------------------- #
def test_naked_margin_far_below_full_cash():
    """Reg-T naked margin per contract must be a fraction (not the full strike*100)."""
    acct = FakeAccount(spot=250.0)
    full_cash = 225.0 * 100.0
    margin = acct.naked_margin_per_contract(225.0, spot=250.0)
    assert 0 < margin < full_cash
    # With spot 250 / strike 225 (25 OTM): max(0.20*250-25, 0.10*250)*100 = max(25,25)*100 = 2500
    assert margin == pytest.approx(2500.0)
    # Without a spot, falls back to 0.20*strike*100 = 4500 (still << full 22500 cash).
    assert acct.naked_margin_per_contract(225.0) == pytest.approx(4500.0)


def test_naked_reserve_uses_margin_not_full_cash():
    """option_reserve_required for naked structures must use the margin model."""
    acct = FakeAccount(spot=250.0)
    full_cash = 225.0 * 100.0 * 2
    for strat in ("short_straddle", "short_strangle", "naked_put", "put_ratio_spread"):
        r = acct.option_reserve_required(strat, 2, strike=225.0, spot=250.0)
        assert 0 < r < full_cash, f"{strat} reserve {r} should be margin (< full cash {full_cash})"
    # cash-secured put stays fully secured (full strike*100 by design).
    assert acct.option_reserve_required("cash_secured_put", 1, strike=225.0) == pytest.approx(22500.0)
