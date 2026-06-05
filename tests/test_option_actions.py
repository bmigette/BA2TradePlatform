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
