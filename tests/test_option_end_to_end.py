"""End-to-end tests for option rulesets driven through TradeActionEvaluator (Phase 2 Task 10).

Each scenario builds a real Ruleset + EventAction (with the JSON trigger/action
shapes used in production), then runs evaluate() + execute(submit_to_broker=True)
against a MockAccount and asserts the broker-facing call (submit_option_order /
close_option_position) actually happened. These would fail if the option action
were silently skipped.

Three canonical flows:
1. ENTER_MARKET dip -> buy_call
2. OPEN_POSITIONS covered-call-on-held-long (+ negative: no long -> no submit)
3. OPEN_POSITIONS close_option on a held long-call position
"""
from datetime import date, timedelta

import pytest

from ba2_trade_platform.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_trade_platform.core.types import (
    ExpertActionType, ExpertEventType, ExpertEventRuleType,
    OrderRecommendation, OrderDirection, OrderType, OrderStatus,
    TransactionStatus, AssetClass, OptionRight, AnalysisUseCase,
)
from ba2_trade_platform.core.models import Ruleset, EventAction, TradingOrder, Transaction
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.option_types import OptionContract
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_expert_instance, create_recommendation,
    create_transaction, create_trading_order, link_rule_to_ruleset,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _one_call_chain(underlying="AAPL", strike=150.0, dte=35):
    """A single liquid OTM-ish call with a valid expiry inside a 20-45 DTE window."""
    return [OptionContract(
        symbol="AAPL150C", underlying=underlying, option_type=OptionRight.CALL,
        strike=strike, expiry=date.today() + timedelta(days=dte),
        bid=4.9, ask=5.1, last=5.0,
        implied_volatility=0.30, delta=0.40, gamma=0.02, theta=-0.03, vega=0.1,
        open_interest=2000, volume=250)]


def _capture_submit(monkeypatch, account):
    """Patch submit_option_order to capture its args and return a FILLED order."""
    captured = {}

    def fake_submit(legs, quantity, order_type="limit", limit_price=None,
                    option_strategy=None, expert_recommendation_id=None, transaction_id=None):
        captured.update(called=True, legs=legs, quantity=quantity,
                        option_strategy=option_strategy, limit_price=limit_price)
        return TradingOrder(account_id=account.id, symbol="AAPL", quantity=quantity,
                            side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
                            status=OrderStatus.FILLED)

    monkeypatch.setattr(account, "submit_option_order", fake_submit, raising=False)
    return captured


# ---------------------------------------------------------------------------
# 1. ENTER_MARKET: dip -> buy_call
# ---------------------------------------------------------------------------
def test_enter_market_dip_buys_call_end_to_end(monkeypatch):
    acct_def = create_account_definition()
    account = MockAccount(acct_def.id)
    ei = create_expert_instance(account_id=acct_def.id)
    rec = create_recommendation(instance_id=ei.id, recommended_action=OrderRecommendation.BUY,
                                confidence=75.0)

    monkeypatch.setattr(account, "get_option_chain", lambda *a, **k: _one_call_chain(),
                        raising=False)
    captured = _capture_submit(monkeypatch, account)

    rs = Ruleset(name="Options Dip Entry", type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
                 subtype=AnalysisUseCase.ENTER_MARKET.value)
    rs_id = add_instance(rs)
    ea = EventAction(
        name="Buy call on bullish dip",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        # confidence >= 0 always passes (sample confidence=75)
        triggers={
            "trigger_0": {"event_type": ExpertEventType.F_BULLISH.value},
            "trigger_1": {"event_type": ExpertEventType.N_CONFIDENCE.value,
                          "operator": ">=", "value": 0.0},
        },
        actions={"action_0": {"action_type": ExpertActionType.BUY_CALL.value,
                              "strike_method": "delta", "strike_param": 0.40,
                              "dte_min": 20, "dte_max": 45, "sizing": 2.0,
                              "min_open_interest": 100, "max_spread_pct": 15.0}},
        continue_processing=False,
    )
    ea_id = add_instance(ea)
    link_rule_to_ruleset(rs_id, ea_id, order_index=0)

    ev = TradeActionEvaluator(account=account)
    summaries = ev.evaluate("AAPL", rec, rs_id)
    assert len(summaries) > 0
    assert len(ev.trade_actions) == 1

    results = ev.execute(submit_to_broker=True)
    assert captured.get("called") is True
    assert captured["option_strategy"] == "long_call"
    assert any(r.get("success") for r in results)


# ---------------------------------------------------------------------------
# 2. OPEN_POSITIONS: covered call on a held equity long
# ---------------------------------------------------------------------------
def _seed_equity_long(account, ei, shares=300.0):
    """Create an OPENED equity long: a Transaction + a filled equity BUY order."""
    txn = create_transaction(symbol="AAPL", quantity=shares, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=150.0,
                             expert_id=ei.id)
    order = create_trading_order(
        account_id=account.id, symbol="AAPL", quantity=shares, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=txn.id,
        filled_qty=shares, asset_class=AssetClass.EQUITY, open_price=150.0,
    )
    return txn, order


def _covered_call_ruleset():
    rs = Ruleset(name="Options Covered Call Overlay",
                 type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
                 subtype=AnalysisUseCase.OPEN_POSITIONS.value)
    rs_id = add_instance(rs)
    ea = EventAction(
        name="Sell covered call on held long",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"trigger_0": {"event_type": ExpertEventType.F_HAS_BUY_POSITION.value}},
        actions={"action_0": {"action_type": ExpertActionType.SELL_COVERED_CALL.value,
                              "strike_method": "delta", "strike_param": 0.30,
                              "dte_min": 20, "dte_max": 45, "sizing": 100.0,
                              "min_open_interest": 100, "max_spread_pct": 15.0}},
        continue_processing=False,
    )
    ea_id = add_instance(ea)
    link_rule_to_ruleset(rs_id, ea_id, order_index=0)
    return rs_id


def test_open_positions_covered_call_on_held_long_end_to_end(monkeypatch):
    acct_def = create_account_definition()
    account = MockAccount(acct_def.id)
    ei = create_expert_instance(account_id=acct_def.id)
    rec = create_recommendation(instance_id=ei.id, recommended_action=OrderRecommendation.HOLD)

    txn, _ = _seed_equity_long(account, ei, shares=300.0)

    monkeypatch.setattr(account, "get_option_chain", lambda *a, **k: _one_call_chain(),
                        raising=False)
    captured = _capture_submit(monkeypatch, account)

    rs_id = _covered_call_ruleset()

    ev = TradeActionEvaluator(account=account, instrument_name="AAPL",
                              existing_transactions=[txn])
    summaries = ev.evaluate("AAPL", rec, rs_id, existing_order=None)
    assert len(summaries) > 0
    assert len(ev.trade_actions) == 1

    results = ev.execute(submit_to_broker=True)
    assert captured.get("called") is True
    assert captured["option_strategy"] == "covered_call"
    assert captured["quantity"] == 3  # 300 shares / 100
    assert any(r.get("success") for r in results)


def test_open_positions_covered_call_no_long_does_not_submit(monkeypatch):
    """Without a held equity long, the covered-call rule must not submit an order."""
    acct_def = create_account_definition()
    account = MockAccount(acct_def.id)
    ei = create_expert_instance(account_id=acct_def.id)
    rec = create_recommendation(instance_id=ei.id, recommended_action=OrderRecommendation.HOLD)

    monkeypatch.setattr(account, "get_option_chain", lambda *a, **k: _one_call_chain(),
                        raising=False)
    captured = _capture_submit(monkeypatch, account)

    rs_id = _covered_call_ruleset()

    # No existing transactions: has_buy_position is False -> rule does not fire.
    ev = TradeActionEvaluator(account=account, instrument_name="AAPL",
                              existing_transactions=[])
    summaries = ev.evaluate("AAPL", rec, rs_id, existing_order=None)
    assert len(summaries) == 0
    assert len(ev.trade_actions) == 0

    ev.execute(submit_to_broker=True)
    assert captured.get("called") is not True


# ---------------------------------------------------------------------------
# 3. OPEN_POSITIONS: close an existing long-call option position
# ---------------------------------------------------------------------------
def test_open_positions_close_option_end_to_end(monkeypatch):
    acct_def = create_account_definition()
    account = MockAccount(acct_def.id)
    ei = create_expert_instance(account_id=acct_def.id)
    rec = create_recommendation(instance_id=ei.id, recommended_action=OrderRecommendation.SELL,
                                confidence=75.0)

    # Seed an OPENED long-call option position: Transaction + filled option BUY order.
    contract_symbol = "AAPL241220C00150000"
    txn = create_transaction(symbol="AAPL", quantity=2.0, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=5.0,
                             expert_id=ei.id)
    option_order = create_trading_order(
        account_id=account.id, symbol="AAPL", quantity=2.0, side=OrderDirection.BUY,
        order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED, transaction_id=txn.id,
        filled_qty=2.0, asset_class=AssetClass.OPTION, option_type=OptionRight.CALL,
        contract_symbol=contract_symbol, underlying_symbol="AAPL",
        strike=150.0, expiry=date.today() + timedelta(days=30),
        option_strategy="long_call", open_price=5.0, limit_price=5.0,
    )

    # Capture close_option_position.
    captured = {}

    def fake_close(position, order_type="limit", limit_price=None):
        captured.update(called=True, contract_symbol=position.contract_symbol,
                        quantity=position.quantity, limit_price=limit_price)
        return TradingOrder(account_id=account.id, symbol="AAPL", quantity=position.quantity,
                            side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
                            status=OrderStatus.FILLED)

    monkeypatch.setattr(account, "close_option_position", fake_close, raising=False)

    rs = Ruleset(name="Options Close On Bearish",
                 type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
                 subtype=AnalysisUseCase.OPEN_POSITIONS.value)
    rs_id = add_instance(rs)
    ea = EventAction(
        name="Close option when expert has an option position",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        # has_option_position is True for the seeded long call -> rule fires.
        triggers={"trigger_0": {"event_type": ExpertEventType.F_HAS_OPTION_POSITION.value}},
        actions={"action_0": {"action_type": ExpertActionType.CLOSE_OPTION.value}},
        continue_processing=False,
    )
    ea_id = add_instance(ea)
    link_rule_to_ruleset(rs_id, ea_id, order_index=0)

    # OPEN_POSITIONS: evaluator gets existing_transactions and the resolved option entry
    # order (mirrors TradeManager.process_open_positions). CloseOptionAction reads the
    # contract off this existing_order.
    ev = TradeActionEvaluator(account=account, instrument_name="AAPL",
                              existing_transactions=[txn])
    summaries = ev.evaluate("AAPL", rec, rs_id, existing_order=option_order)
    assert len(summaries) > 0
    assert len(ev.trade_actions) == 1

    results = ev.execute(submit_to_broker=True)
    assert captured.get("called") is True
    assert captured["contract_symbol"] == contract_symbol
    assert any(r.get("success") for r in results)
