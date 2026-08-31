"""``max_loss_per_contract`` is persisted at option-entry submit -- design 2026-08-29 (S8.2).

The exit condition ``loss_pct_of_max_loss`` reads this number BACK off the parent order's
``data`` -- no leg reconstruction, no OCC parsing. That only works if the submit path stamps
it, at the same seam ``option_reserve`` already uses (``_submit_option_order``, the one choke
point every builder reaches).

THE GATE: PERSISTED ONLY WHEN MEASURED. ``option_payoff.max_loss`` is tri-state, and only
``MEASURED`` yields a number. Absence of the key IS the "contracts that support it" rule,
enforced by data rather than by a runtime special case downstream:

* a defined-risk vertical stamps its measured max loss;
* a naked short CALL stamps NOTHING -- its loss is UNBOUNDED, there is no denominator;
* a naked short PUT **STAMPS** -- its loss is BOUNDED (strike minus credit, at underlying
  zero) and therefore MEASURED. This is the corrected S6 rule (2026-08-30): the emission
  predicate is the MEASURED payoff, not "is this a short", and the short put sits exactly
  on the line the correction moved.

Per-leg premiums are gone by submit time (builders pass only the NET limit), but the payoff
at expiry depends on the individual premiums ONLY through their net -- every premium term in
``payoff_at`` is a spot-independent cash flow -- so the derivation carries the net on one
carrier leg and the payoff curve is identical to the true structure's.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import create_action
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_types import OptionContract, OptionLeg
from ba2_common.core.types import ExpertActionType, OptionRight, OrderDirection

TODAY = date(2024, 6, 1)
EXPIRY = date(2024, 6, 21)


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """These tests persist TradeActionResult rows. Sibling DB-seam tests repoint the
    global DB seam at their own temp sqlite without restoring it, so re-point to a
    fresh, fully-initialized sqlite for each test here (order-independence)."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "max_loss_at_submit.sqlite"))
    db.init_db()
    yield


class FakeAccount(OptionsAccountInterface):
    """Minimal options account capturing ``submit_option_order`` calls.

    Chain: strikes 80..120 step 5, both rights, one expiry, premium = intrinsic + time
    value decaying with OTM distance (same model as test_bull_put_spread, so verticals
    come out as genuine net credits/debits)."""

    def __init__(self, spot=100.0, balance=100_000.0):
        self.id = 1
        self._spot = spot
        self._balance = balance
        self.submitted = []
        #: id the next submit_option_order returns; lets a test point the persistence
        #: read-modify-write at a REAL TradingOrder row it seeded.
        self.next_order_id = 1

    def _as_of_date(self):
        return TODAY

    def get_balance(self):
        return self._balance

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=self._balance, equity=self._balance,
                               net_liquidation=self._balance)

    def get_instrument_current_price(self, symbol, price_type=None):
        return self._spot

    def get_current_price(self, symbol=None):
        return self._spot

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        for s in range(80, 121, 5):
            if option_type == OptionRight.CALL:
                otm_dist = max(float(s) - self._spot, 0.0)
                intrinsic = max(self._spot - float(s), 0.0)
            else:
                otm_dist = max(self._spot - float(s), 0.0)
                intrinsic = max(float(s) - self._spot, 0.0)
            bid = max(0.2, 5.0 - 0.08 * otm_dist) + intrinsic
            out.append(OptionContract(
                symbol=f"{underlying}{s}{'C' if option_type == OptionRight.CALL else 'P'}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=EXPIRY, bid=round(bid, 4), ask=round(bid + 0.2, 4),
                last=round(bid, 4), open_interest=1000, delta=None,
                implied_volatility=None))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(dict(legs=legs, quantity=quantity,
                                   limit_price=limit_price, strategy=option_strategy,
                                   order_type=order_type))
        return SimpleNamespace(id=self.next_order_id, data={})

    # --- unused abstract bits
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


def act(acct, action_type, **kw):
    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    a = create_action(ExpertActionType(action_type), "XYZ", acct,
                      SimpleNamespace(), None, rec, **kw)
    a.submit_to_broker = True
    return a


BPS = dict(strike_method="percent_otm", strike_param=[5.0, 10.0], dte_min=10, dte_max=40,
           sizing=5.0)


def _short_call_leg(strike=100.0):
    return OptionLeg(contract_symbol=f"XYZ{strike:g}C", side=OrderDirection.SELL,
                     position_intent="sell_to_open", option_type=OptionRight.CALL,
                     strike=strike, expiry=EXPIRY, underlying="XYZ")


def _short_put_leg(strike=100.0):
    return OptionLeg(contract_symbol=f"XYZ{strike:g}P", side=OrderDirection.SELL,
                     position_intent="sell_to_open", option_type=OptionRight.PUT,
                     strike=strike, expiry=EXPIRY, underlying="XYZ")


# ==========================================================================
# defined risk stamps; the measured number, per ONE contract
# ==========================================================================
def test_a_defined_risk_submit_stamps_the_measured_max_loss():
    """Bull put spread: max loss of ONE contract = (width - credit) x 100. PER CONTRACT,
    not total -- the exit condition multiplies by the position's contract count itself,
    which is what makes the resulting percentage scale-free."""
    acct = FakeAccount()
    res = act(acct, "open_bull_put_spread", **BPS).execute()
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    short = [l for l in sub["legs"] if l.side == OrderDirection.SELL][0]
    long_ = [l for l in sub["legs"] if l.side == OrderDirection.BUY][0]
    width = short.strike - long_.strike
    credit = -sub["limit_price"]
    assert res["data"]["max_loss_per_contract"] == pytest.approx((width - credit) * 100.0)


def test_a_debit_vertical_stamps_max_loss_equal_to_its_net_debit():
    """The converted (ResolvedStructure) path reaches the same seam: a bull call
    spread's max loss is the debit paid."""
    acct = FakeAccount()
    res = act(acct, "open_bull_call_spread", strike_method="percent_otm",
              strike_param=[0.0, 10.0], dte_min=10, dte_max=40, sizing=5.0).execute()
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    debit = sub["limit_price"]
    assert debit > 0
    assert res["data"]["max_loss_per_contract"] == pytest.approx(debit * 100.0)


def test_the_stamp_reaches_the_stored_order_row_beside_option_reserve():
    """The read-back seam: the condition evaluator reads ``TradingOrder.data``, so the
    stamp must land on the ROW, exactly as ``option_reserve`` does (design S8.2)."""
    from ba2_common.core.db import add_instance, get_instance
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderStatus, OrderType

    row = TradingOrder(account_id=1, symbol="XYZ", quantity=1,
                       side=OrderDirection.SELL, order_type=OrderType.MARKET,
                       status=OrderStatus.PENDING)
    row_id = add_instance(row)

    acct = FakeAccount()
    acct.next_order_id = row_id
    res = act(acct, "open_bull_put_spread", **BPS).execute()
    assert res["success"], res["message"]

    stored = get_instance(TradingOrder, row_id)
    assert stored.data["max_loss_per_contract"] == pytest.approx(
        res["data"]["max_loss_per_contract"])
    assert "option_reserve" in stored.data  # the neighbour this stamp mirrors


# ==========================================================================
# the MEASURED gate -- what stamps nothing, and the corrected-S6 short put
# ==========================================================================
def test_a_naked_short_call_submit_stamps_nothing_THE_MEASURED_GATE():
    """UNBOUNDED loss -> no number -> NO KEY. Mutation target: a persist path that stamps
    whatever ``max_loss`` returned (or a made-up default) writes a key here, and an
    absent-denominator downstream would read a fabricated risk. The seam is exercised
    directly because the only naked-short-call builder (covered call) carries its stock
    cover outside the order's own legs."""
    acct = FakeAccount()
    a = act(acct, "open_bull_put_spread", **BPS)
    res = a._submit_option_order([_short_call_leg()], 1, 3.0, "covered_call")
    assert res["success"], res["message"]
    assert "max_loss_per_contract" not in res["data"]


def test_a_naked_short_put_submit_STAMPS_its_measured_max_loss_corrected_s6():
    """THE CORRECTED S6 CASE (2026-08-30). A naked short put's loss is BOUNDED -- strike
    minus credit, at underlying zero -- so its max loss is MEASURED and it STAMPS. The
    predicate is the measured payoff, never 'is this a naked short'. Runs the REAL
    cash-secured-put builder end to end."""
    acct = FakeAccount()
    res = act(acct, "sell_cash_secured_put", strike_method="percent_otm",
              strike_param=5.0, dte_min=10, dte_max=40, sizing=30.0).execute()
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    leg = sub["legs"][0]
    assert leg.side == OrderDirection.SELL and leg.option_type == OptionRight.PUT
    credit = sub["limit_price"]           # single-leg SELL: positive premium, credit
    assert credit > 0
    assert res["data"]["max_loss_per_contract"] == pytest.approx(
        (leg.strike - credit) * 100.0)


def test_a_short_put_STAMPS_and_a_short_call_does_not_at_the_same_seam():
    """The corrected-S6 pair side by side, all else equal: same strike, same credit, same
    seam -- only the payoff shape differs, and only the put is bounded."""
    acct = FakeAccount()
    a = act(acct, "open_bull_put_spread", **BPS)
    res_put = a._submit_option_order([_short_put_leg(100.0)], 1, 4.0, "cash_secured_put")
    res_call = a._submit_option_order([_short_call_leg(100.0)], 1, 4.0, "covered_call")
    assert res_put["data"]["max_loss_per_contract"] == pytest.approx((100.0 - 4.0) * 100.0)
    assert "max_loss_per_contract" not in res_call["data"]


def test_an_underivable_leg_stamps_nothing_absence_never_a_guess():
    """A leg with no strike/right cannot be priced: the stamp is ABSENT, never a
    fabricated number. (Unknown must never read as a denominator downstream.)"""
    acct = FakeAccount()
    a = act(acct, "open_bull_put_spread", **BPS)
    bare = OptionLeg(contract_symbol="XYZ???", side=OrderDirection.SELL)
    res = a._submit_option_order([bare], 1, 4.0, "cash_secured_put")
    assert res["success"], res["message"]
    assert "max_loss_per_contract" not in res["data"]


def test_a_ratio_structure_carries_the_net_on_the_carrier_leg_correctly():
    """Put ratio spread (buy 1 high put, sell 2 low puts): the carrier is the ratio-2
    short leg, so its synthetic premium must be net/2 or the credit is double-counted.
    Asserted against option_payoff's own answer on an equivalent leg set carrying the
    same net, rather than a re-derivation here."""
    from ba2_common.core.option_payoff import MEASURED, PayoffLeg, max_loss

    acct = FakeAccount()
    a = act(acct, "open_put_ratio_spread", strike_method="percent_otm", strike_param=5.0,
            dte_min=10, dte_max=40, sizing=20.0, wing_width_pct=5.0)
    res = a.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    long_ = [l for l in sub["legs"] if l.side == OrderDirection.BUY][0]
    short = [l for l in sub["legs"] if l.side == OrderDirection.SELL][0]
    assert short.ratio_qty == 2
    net = sub["limit_price"]              # signed: long.ask - 2*short.bid
    truth_legs = [
        PayoffLeg(kind="put", side=OrderDirection.BUY,
                  premium=net if net >= 0 else 0.0, strike=long_.strike),
        PayoffLeg(kind="put", side=OrderDirection.SELL,
                  premium=(-net / 2.0) if net < 0 else 0.0, strike=short.strike, ratio=2),
    ]
    truth = max_loss(truth_legs)
    assert truth.state == MEASURED
    assert res["data"]["max_loss_per_contract"] == pytest.approx(truth.amount)
