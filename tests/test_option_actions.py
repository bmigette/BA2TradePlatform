"""Tests for option rule ACTIONS (Phase 2 Task 5).

Covers BuyCallAction, OpenBullCallSpreadAction, SellCoveredCallAction, and
CloseOptionAction created via create_action(). Each test monkeypatches the
account's get_option_chain to a KNOWN chain and captures submit_option_order /
close_option_position to assert legs, pricing (buy@ask / sell@bid), strategy
tags, and pct_equity sizing.
"""
from datetime import date, timedelta
import pytest

from ba2_trade_platform.core.TradeActions import create_action
from ba2_trade_platform.core.option_types import OptionContract, OptionPosition
from ba2_trade_platform.core.types import (
    ExpertActionType, OptionRight, OrderDirection, OrderRecommendation,
    OrderStatus, OrderType, TransactionStatus, AssetClass,
)
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import Transaction, TradingOrder


def _exp(days):
    return date.today() + timedelta(days=days)


def _call(strike, *, delta=0.5, bid=2.0, ask=2.2, oi=1000, exp=None):
    return OptionContract(symbol=f"AAPL{int(strike)}C", underlying="AAPL",
        option_type=OptionRight.CALL, strike=float(strike), expiry=exp or _exp(35),
        bid=bid, ask=ask, last=(bid+ask)/2, implied_volatility=0.3, delta=delta,
        gamma=0.0, theta=0.0, vega=0.0, open_interest=oi, volume=100)


def _put(strike, *, delta=-0.5, bid=2.0, ask=2.2, oi=1000, exp=None):
    return OptionContract(symbol=f"AAPL{int(strike)}P", underlying="AAPL",
        option_type=OptionRight.PUT, strike=float(strike), expiry=exp or _exp(35),
        bid=bid, ask=ask, last=(bid+ask)/2, implied_volatility=0.3, delta=delta,
        gamma=0.0, theta=0.0, vega=0.0, open_interest=oi, volume=100)


def _capture_submit(monkeypatch, account):
    captured = {}
    def fake(legs, quantity, order_type="limit", limit_price=None, option_strategy=None,
             expert_recommendation_id=None, transaction_id=None):
        captured.update(legs=legs, quantity=quantity, order_type=order_type,
                        limit_price=limit_price, option_strategy=option_strategy)
        return TradingOrder(account_id=account.id, symbol="AAPL", quantity=quantity,
                            side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
                            status=OrderStatus.FILLED)
    monkeypatch.setattr(account, "submit_option_order", fake, raising=False)
    return captured


def test_buy_call_submits_long_call(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    chain = [_call(150, delta=0.50, bid=4.9, ask=5.1, oi=2000)]
    monkeypatch.setattr(mock_account, "get_option_chain", lambda *a, **k: chain, raising=False)
    cap = _capture_submit(monkeypatch, mock_account)
    action = create_action(action_type=ExpertActionType.BUY_CALL, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.BUY,
        existing_order=None, expert_recommendation=sample_recommendation,
        strike_method="delta", strike_param=0.50, dte_min=20, dte_max=45,
        sizing=2.0, min_open_interest=100, max_spread_pct=20.0)
    res = action.execute()
    assert res["success"] is True
    assert len(cap["legs"]) == 1
    leg = cap["legs"][0]
    assert leg.option_type == OptionRight.CALL and leg.side == OrderDirection.BUY
    assert leg.position_intent == "buy_to_open"
    assert cap["option_strategy"] == "long_call"
    assert cap["limit_price"] == 5.1                       # buy at ASK
    # budget = 100000 * 100% * 2% = 2000; qty = floor(2000/(5.1*100)) = 3
    assert cap["quantity"] == 3


def test_buy_call_no_liquid_contract_skips(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    chain = [_call(150, oi=5, bid=2.0, ask=4.0)]   # low OI + wide spread
    monkeypatch.setattr(mock_account, "get_option_chain", lambda *a, **k: chain, raising=False)
    cap = {}
    monkeypatch.setattr(mock_account, "submit_option_order",
                        lambda *a, **k: cap.update(called=True), raising=False)
    action = create_action(action_type=ExpertActionType.BUY_CALL, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.BUY, existing_order=None,
        expert_recommendation=sample_recommendation, strike_method="delta", strike_param=0.50,
        dte_min=20, dte_max=45, sizing=2.0, min_open_interest=100, max_spread_pct=10.0)
    res = action.execute()
    assert res["success"] is False
    assert "called" not in cap                              # never submitted


def test_bull_call_spread_two_legs(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    chain = [_call(150, delta=0.55, bid=5.9, ask=6.1, oi=2000),
             _call(160, delta=0.30, bid=1.9, ask=2.1, oi=2000)]
    monkeypatch.setattr(mock_account, "get_option_chain", lambda *a, **k: chain, raising=False)
    cap = _capture_submit(monkeypatch, mock_account)
    action = create_action(action_type=ExpertActionType.OPEN_BULL_CALL_SPREAD, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.BUY, existing_order=None,
        expert_recommendation=sample_recommendation, strike_method="delta",
        strike_param={"long": 0.55, "short": 0.30}, dte_min=20, dte_max=45,
        sizing=5.0, min_open_interest=100, max_spread_pct=20.0)
    res = action.execute()
    assert res["success"] is True
    assert len(cap["legs"]) == 2
    long_leg, short_leg = cap["legs"]
    assert long_leg.side == OrderDirection.BUY and long_leg.position_intent == "buy_to_open"
    assert short_leg.side == OrderDirection.SELL and short_leg.position_intent == "sell_to_open"
    assert long_leg.strike == 150.0 and short_leg.strike == 160.0
    assert cap["option_strategy"] == "bull_call_spread"
    # net debit = long.ask - short.bid = 6.1 - 1.9 = 4.2
    assert abs(cap["limit_price"] - 4.2) < 1e-9
    # budget 100000*5% = 5000; qty = floor(5000/(4.2*100)) = 11
    assert cap["quantity"] == 11


def test_buy_put_submits_long_put(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    chain = [_put(150, delta=-0.50, bid=4.9, ask=5.1, oi=2000)]
    monkeypatch.setattr(mock_account, "get_option_chain", lambda *a, **k: chain, raising=False)
    cap = _capture_submit(monkeypatch, mock_account)
    action = create_action(action_type=ExpertActionType.BUY_PUT, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.SELL,
        existing_order=None, expert_recommendation=sample_recommendation,
        strike_method="delta", strike_param=0.50, dte_min=20, dte_max=45,
        sizing=2.0, min_open_interest=100, max_spread_pct=20.0)
    res = action.execute()
    assert res["success"] is True
    assert len(cap["legs"]) == 1
    leg = cap["legs"][0]
    assert leg.option_type == OptionRight.PUT and leg.side == OrderDirection.BUY
    assert leg.position_intent == "buy_to_open"
    assert cap["option_strategy"] == "long_put"
    assert cap["limit_price"] == 5.1                       # buy at ASK
    # budget = 100000 * 100% * 2% = 2000; qty = floor(2000/(5.1*100)) = 3
    assert cap["quantity"] == 3


def test_bear_put_spread_two_legs(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    # Bear put debit spread: BUY higher strike (bigger |delta|), SELL lower strike.
    chain = [_put(160, delta=-0.55, bid=5.9, ask=6.1, oi=2000),
             _put(150, delta=-0.30, bid=1.9, ask=2.1, oi=2000)]
    monkeypatch.setattr(mock_account, "get_option_chain", lambda *a, **k: chain, raising=False)
    cap = _capture_submit(monkeypatch, mock_account)
    action = create_action(action_type=ExpertActionType.OPEN_BEAR_PUT_SPREAD, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.SELL, existing_order=None,
        expert_recommendation=sample_recommendation, strike_method="delta",
        strike_param={"long": 0.55, "short": 0.30}, dte_min=20, dte_max=45,
        sizing=5.0, min_open_interest=100, max_spread_pct=20.0)
    res = action.execute()
    assert res["success"] is True
    assert len(cap["legs"]) == 2
    long_leg, short_leg = cap["legs"]
    assert long_leg.side == OrderDirection.BUY and long_leg.position_intent == "buy_to_open"
    assert short_leg.side == OrderDirection.SELL and short_leg.position_intent == "sell_to_open"
    # For a PUT debit spread the long is the HIGHER strike, the short the LOWER.
    assert long_leg.strike == 160.0 and short_leg.strike == 150.0
    assert cap["option_strategy"] == "bear_put_spread"
    # net debit = long.ask - short.bid = 6.1 - 1.9 = 4.2
    assert abs(cap["limit_price"] - 4.2) < 1e-9
    # budget 100000*5% = 5000; qty = floor(5000/(4.2*100)) = 11
    assert cap["quantity"] == 11


def test_sell_covered_call_requires_long(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    # No held long -> skip, no submit
    chain = [_call(160, delta=0.30, bid=2.0, ask=2.2, oi=2000)]
    monkeypatch.setattr(mock_account, "get_option_chain", lambda *a, **k: chain, raising=False)
    submitted = {}
    monkeypatch.setattr(mock_account, "submit_option_order", lambda *a, **k: submitted.update(x=1), raising=False)
    action = create_action(action_type=ExpertActionType.SELL_COVERED_CALL, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.HOLD, existing_order=None,
        expert_recommendation=sample_recommendation, strike_method="percent_otm", strike_param=5.0,
        dte_min=20, dte_max=45, min_open_interest=100, max_spread_pct=20.0)
    res = action.execute()
    assert res["success"] is False and "x" not in submitted

    # With a held 300-share equity long -> 3 contracts
    txn_id = add_instance(Transaction(symbol="AAPL", quantity=300, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=150.0, expert_id=mock_expert_instance.id))
    add_instance(TradingOrder(account_id=mock_account.id, symbol="AAPL", quantity=300,
        side=OrderDirection.BUY, order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        filled_qty=300, open_price=150.0, transaction_id=txn_id))  # equity (asset_class defaults EQUITY)
    cap = _capture_submit(monkeypatch, mock_account)
    res2 = action.execute()
    assert res2["success"] is True
    assert cap["quantity"] == 3                              # floor(300/100)
    assert cap["legs"][0].side == OrderDirection.SELL
    assert cap["legs"][0].position_intent == "sell_to_open"
    assert cap["option_strategy"] == "covered_call"
    assert cap["limit_price"] == 2.0                         # sell at BID


def test_buy_protective_put_requires_long(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    # No held long -> skip, no submit
    chain = [_put(140, delta=-0.30, bid=2.0, ask=2.2, oi=2000)]
    monkeypatch.setattr(mock_account, "get_option_chain", lambda *a, **k: chain, raising=False)
    submitted = {}
    monkeypatch.setattr(mock_account, "submit_option_order", lambda *a, **k: submitted.update(x=1), raising=False)
    action = create_action(action_type=ExpertActionType.BUY_PROTECTIVE_PUT, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.HOLD, existing_order=None,
        expert_recommendation=sample_recommendation, strike_method="percent_otm", strike_param=5.0,
        dte_min=20, dte_max=45, min_open_interest=100, max_spread_pct=20.0)
    res = action.execute()
    assert res["success"] is False and "x" not in submitted

    # With a held 200-share equity long -> 2 contracts
    txn_id = add_instance(Transaction(symbol="AAPL", quantity=200, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=150.0, expert_id=mock_expert_instance.id))
    add_instance(TradingOrder(account_id=mock_account.id, symbol="AAPL", quantity=200,
        side=OrderDirection.BUY, order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        filled_qty=200, open_price=150.0, transaction_id=txn_id))  # equity (asset_class defaults EQUITY)
    cap = _capture_submit(monkeypatch, mock_account)
    res2 = action.execute()
    assert res2["success"] is True
    assert cap["quantity"] == 2                              # floor(200/100)
    assert cap["legs"][0].option_type == OptionRight.PUT
    assert cap["legs"][0].side == OrderDirection.BUY
    assert cap["legs"][0].position_intent == "buy_to_open"
    assert cap["option_strategy"] == "protective_put"
    assert cap["limit_price"] == 2.2                         # buy at ASK


def test_buy_call_sizing_respects_virtual_equity_pct(monkeypatch, mock_account_def):
    from tests.factories import create_expert_instance, create_recommendation
    from tests.conftest import MockAccount
    ei = create_expert_instance(account_id=mock_account_def.id, expert="MockExpert",
                                virtual_equity_pct=50.0)
    rec = create_recommendation(instance_id=ei.id, symbol="AAPL")
    chain = [_call(150, delta=0.50, bid=4.9, ask=5.1, oi=2000)]
    acct = MockAccount(mock_account_def.id)
    monkeypatch.setattr(acct, "get_option_chain", lambda *a, **k: chain, raising=False)
    cap = _capture_submit(monkeypatch, acct)
    action = create_action(action_type=ExpertActionType.BUY_CALL, instrument_name="AAPL",
        account=acct, order_recommendation=OrderRecommendation.BUY, existing_order=None,
        expert_recommendation=rec, strike_method="delta", strike_param=0.50,
        dte_min=20, dte_max=45, sizing=2.0, min_open_interest=100, max_spread_pct=20.0)
    res = action.execute()
    assert res["success"] is True
    # budget = 100000 * 50% * 2% = 1000; qty = floor(1000/(5.1*100)) = 1  (vs 3 at 100%)
    assert cap["quantity"] == 1


def _capture_submit_persisted(monkeypatch, account):
    """Like _capture_submit but PERSISTS the returned order (gives it an id) so
    the action can store data['option_reserve'] via update_instance and tests can
    re-fetch it."""
    captured = {}

    def fake(legs, quantity, order_type="limit", limit_price=None, option_strategy=None,
             expert_recommendation_id=None, transaction_id=None):
        captured.update(legs=legs, quantity=quantity, order_type=order_type,
                        limit_price=limit_price, option_strategy=option_strategy)
        order = TradingOrder(account_id=account.id, symbol="AAPL", quantity=quantity,
                             side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
                             status=OrderStatus.FILLED, asset_class=AssetClass.OPTION,
                             option_strategy=option_strategy)
        oid = add_instance(order)
        from ba2_trade_platform.core.db import get_instance
        captured["order_id"] = oid
        return get_instance(TradingOrder, oid)

    monkeypatch.setattr(account, "submit_option_order", fake, raising=False)
    return captured


def test_cash_secured_put_reserves_and_sizes(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    # Sell-to-open a PUT @ BID; reserve = strike*100 per contract.
    chain = [_put(140, delta=-0.30, bid=3.0, ask=3.2, oi=2000)]
    monkeypatch.setattr(mock_account, "get_option_chain", lambda *a, **k: chain, raising=False)
    cap = _capture_submit_persisted(monkeypatch, mock_account)
    action = create_action(action_type=ExpertActionType.SELL_CASH_SECURED_PUT, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.HOLD, existing_order=None,
        expert_recommendation=sample_recommendation, strike_method="percent_otm", strike_param=5.0,
        dte_min=20, dte_max=45, sizing=50.0, min_open_interest=100, max_spread_pct=20.0)
    res = action.execute()
    assert res["success"] is True
    assert len(cap["legs"]) == 1
    leg = cap["legs"][0]
    assert leg.option_type == OptionRight.PUT and leg.side == OrderDirection.SELL
    assert leg.position_intent == "sell_to_open"
    assert leg.strike == 140.0
    assert cap["option_strategy"] == "cash_secured_put"
    assert cap["limit_price"] == 3.0                        # sell at BID
    # budget = 100000 * 100% * 50% = 50000; per-contract reserve = 140*100 = 14000
    # qty = floor(50000 / 14000) = 3
    assert cap["quantity"] == 3
    # reserve persisted on the order: strike*100*qty = 140*100*3 = 42000
    from ba2_trade_platform.core.db import get_instance
    order = get_instance(TradingOrder, cap["order_id"])
    assert (order.data or {}).get("option_reserve") == 140.0 * 100.0 * 3


def test_cash_secured_put_insufficient_bp_skips(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    # Seed an existing OPEN option order reserving most of the balance so the CSP
    # reserve no longer fits in available buying power -> skip, no submit.
    add_instance(TradingOrder(account_id=mock_account.id, symbol="MSFT", quantity=10,
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        data={"option_reserve": 95000.0}))
    chain = [_put(140, delta=-0.30, bid=3.0, ask=3.2, oi=2000)]
    monkeypatch.setattr(mock_account, "get_option_chain", lambda *a, **k: chain, raising=False)
    submitted = {}
    monkeypatch.setattr(mock_account, "submit_option_order",
                        lambda *a, **k: submitted.update(x=1), raising=False)
    action = create_action(action_type=ExpertActionType.SELL_CASH_SECURED_PUT, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.HOLD, existing_order=None,
        expert_recommendation=sample_recommendation, strike_method="percent_otm", strike_param=5.0,
        dte_min=20, dte_max=45, sizing=50.0, min_open_interest=100, max_spread_pct=20.0)
    res = action.execute()
    assert res["success"] is False
    assert "buying power" in res["message"].lower()
    assert "x" not in submitted                             # never submitted


def test_bear_call_spread_credit(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    # Bear CALL credit spread: SHORT the lower strike (150), LONG the higher strike (160).
    chain = [_call(150, delta=0.35, bid=4.0, ask=4.2, oi=2000),
             _call(160, delta=0.18, bid=1.8, ask=2.0, oi=2000)]
    monkeypatch.setattr(mock_account, "get_option_chain", lambda *a, **k: chain, raising=False)
    cap = _capture_submit_persisted(monkeypatch, mock_account)
    action = create_action(action_type=ExpertActionType.OPEN_BEAR_CALL_SPREAD, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.SELL, existing_order=None,
        expert_recommendation=sample_recommendation, strike_method="delta",
        strike_param={"long": 0.18, "short": 0.35}, dte_min=20, dte_max=45,
        sizing=50.0, min_open_interest=100, max_spread_pct=20.0)
    res = action.execute()
    assert res["success"] is True
    assert len(cap["legs"]) == 2
    short_leg, long_leg = cap["legs"]
    # SHORT = lower strike (150), SELL/sell_to_open
    assert short_leg.strike == 150.0
    assert short_leg.side == OrderDirection.SELL and short_leg.position_intent == "sell_to_open"
    assert short_leg.option_type == OptionRight.CALL
    # LONG = higher strike (160), BUY/buy_to_open (protection)
    assert long_leg.strike == 160.0
    assert long_leg.side == OrderDirection.BUY and long_leg.position_intent == "buy_to_open"
    assert long_leg.option_type == OptionRight.CALL
    assert cap["option_strategy"] == "bear_call_spread"
    # net_credit = short.bid - long.ask = 4.0 - 2.0 = 2.0; width = 160 - 150 = 10
    # limit_price NEGATIVE (credit) = -2.0
    assert abs(cap["limit_price"] - (-2.0)) < 1e-9
    # per-spread reserve = (width - net_credit)*100 = (10-2)*100 = 800
    # budget = 100000 * 50% = 50000; qty = floor(50000/800) = 62
    qty = cap["quantity"]
    assert qty == 62
    # reserve persisted = max_loss = (width - net_credit)*100*qty = 800*62
    from ba2_trade_platform.core.db import get_instance
    order = get_instance(TradingOrder, cap["order_id"])
    assert (order.data or {}).get("option_reserve") == (10.0 - 2.0) * 100.0 * qty


def _capture_chain_by_right(monkeypatch, account, call_chain, put_chain):
    """Patch get_option_chain to return CALL or PUT chain based on option_type arg.

    _chain() calls get_option_chain(symbol, expiry_min, expiry_max, option_type) so
    the 4th positional (or 'option_type' kw) selects which canned chain to return.
    """
    def fake(*args, **kwargs):
        opt = kwargs.get("option_type")
        if opt is None and len(args) >= 4:
            opt = args[3]
        if opt == OptionRight.PUT:
            return put_chain
        return call_chain
    monkeypatch.setattr(account, "get_option_chain", fake, raising=False)


def test_open_straddle_two_legs(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    # ATM straddle: BUY a call AND a put at the SAME strike nearest spot (150).
    call_chain = [_call(145, bid=6.9, ask=7.1, oi=2000),
                  _call(150, bid=4.9, ask=5.1, oi=2000),
                  _call(155, bid=2.9, ask=3.1, oi=2000)]
    put_chain = [_put(145, bid=2.9, ask=3.1, oi=2000),
                 _put(150, bid=4.4, ask=4.6, oi=2000),
                 _put(155, bid=6.9, ask=7.1, oi=2000)]
    _capture_chain_by_right(monkeypatch, mock_account, call_chain, put_chain)
    cap = _capture_submit(monkeypatch, mock_account)
    action = create_action(action_type=ExpertActionType.OPEN_STRADDLE, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.BUY, existing_order=None,
        expert_recommendation=sample_recommendation, strike_method="percent_otm",
        strike_param=0, dte_min=20, dte_max=45, sizing=10.0,
        min_open_interest=100, max_spread_pct=20.0)
    res = action.execute()
    assert res["success"] is True
    assert len(cap["legs"]) == 2
    call_leg = next(l for l in cap["legs"] if l.option_type == OptionRight.CALL)
    put_leg = next(l for l in cap["legs"] if l.option_type == OptionRight.PUT)
    # SAME strike (150) for both legs
    assert call_leg.strike == 150.0 and put_leg.strike == 150.0
    # both legs BUY / buy_to_open
    assert call_leg.side == OrderDirection.BUY and call_leg.position_intent == "buy_to_open"
    assert put_leg.side == OrderDirection.BUY and put_leg.position_intent == "buy_to_open"
    assert cap["option_strategy"] == "straddle"
    # net debit = call.ask + put.ask = 5.1 + 4.6 = 9.7 (positive)
    assert abs(cap["limit_price"] - 9.7) < 1e-9
    # budget = 100000 * 10% = 10000; qty = floor(10000/(9.7*100)) = 10
    assert cap["quantity"] == 10


def test_open_strangle_two_legs(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    # OTM strangle: BUY an OTM call (above spot) AND an OTM put (below spot), DIFFERENT strikes.
    # spot=150, 5% OTM -> call target 157.5 (nearest 155), put target 142.5 (nearest 145).
    call_chain = [_call(150, bid=4.9, ask=5.1, oi=2000),
                  _call(155, bid=2.9, ask=3.1, oi=2000)]
    put_chain = [_put(150, bid=4.4, ask=4.6, oi=2000),
                 _put(145, bid=2.4, ask=2.6, oi=2000)]
    _capture_chain_by_right(monkeypatch, mock_account, call_chain, put_chain)
    cap = _capture_submit(monkeypatch, mock_account)
    action = create_action(action_type=ExpertActionType.OPEN_STRANGLE, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.BUY, existing_order=None,
        expert_recommendation=sample_recommendation, strike_method="percent_otm",
        strike_param=5.0, dte_min=20, dte_max=45, sizing=10.0,
        min_open_interest=100, max_spread_pct=20.0)
    res = action.execute()
    assert res["success"] is True
    assert len(cap["legs"]) == 2
    call_leg = next(l for l in cap["legs"] if l.option_type == OptionRight.CALL)
    put_leg = next(l for l in cap["legs"] if l.option_type == OptionRight.PUT)
    # OTM call ABOVE spot, OTM put BELOW spot -> DIFFERENT strikes
    assert call_leg.strike == 155.0 and put_leg.strike == 145.0
    assert call_leg.side == OrderDirection.BUY and call_leg.position_intent == "buy_to_open"
    assert put_leg.side == OrderDirection.BUY and put_leg.position_intent == "buy_to_open"
    assert cap["option_strategy"] == "strangle"
    # net debit = call.ask + put.ask = 3.1 + 2.6 = 5.7
    assert abs(cap["limit_price"] - 5.7) < 1e-9
    # budget = 100000 * 10% = 10000; qty = floor(10000/(5.7*100)) = 17
    assert cap["quantity"] == 17


def test_close_option_calls_close(monkeypatch, mock_account, mock_expert_instance, sample_recommendation):
    # Seed an OPEN long call option order on a transaction; CloseOption should close it.
    txn_id = add_instance(Transaction(symbol="AAPL", quantity=2, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=5.0, expert_id=mock_expert_instance.id))
    entry = TradingOrder(account_id=mock_account.id, symbol="AAPL", quantity=2,
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED,
        filled_qty=2, open_price=5.0, transaction_id=txn_id, asset_class=AssetClass.OPTION,
        option_type=OptionRight.CALL, strike=150.0, expiry=_exp(35), underlying_symbol="AAPL",
        contract_symbol="AAPL150C", option_strategy="long_call")
    entry_id = add_instance(entry)
    from ba2_trade_platform.core.db import get_instance
    entry = get_instance(TradingOrder, entry_id)
    closed = {}
    monkeypatch.setattr(mock_account, "close_option_position",
        lambda position, order_type="limit", limit_price=None: closed.update(
            position=position, limit_price=limit_price) or "CLOSED", raising=False)
    action = create_action(action_type=ExpertActionType.CLOSE_OPTION, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.SELL,
        existing_order=entry, expert_recommendation=sample_recommendation)
    res = action.execute()
    assert res["success"] is True
    assert closed["position"].contract_symbol == "AAPL150C"
    assert closed["position"].side == OrderDirection.BUY    # long position
