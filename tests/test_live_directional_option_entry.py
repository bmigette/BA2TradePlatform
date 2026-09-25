"""A DIRECTIONAL live option entry sizes itself, exactly like the backtest option path.

The regression (8082 options-testing, 2026-09-24): the first live entry pass of a deployed stage-1
O_LC genome (BUY signal, ``buy_call`` action, ``evaluate_entry_rules_on_hold`` off) routed the
option action through the EQUITY funded-entry flow. The option action had already sized and sent
2 contracts, then the equity risk manager re-labelled the entry "9 shares", rewrote the transaction
quantity 2 -> 9 and staged a STOCK safeguard stop (SELL 9 GILD @ 138.56) that went live at the
broker against a position that holds no shares. Only a HOLD-admitting expert took the option path;
the backtest (``daily_engine._entry_is_option``) routes every option entry there regardless of HOLD.

Pinned here with the real evaluator/actions and a mock broker:
* a BUY-signal option entry never reaches the equity risk manager, and its orders and transaction
  carry the CONTRACT quantity with no equity stop leg;
* a rule mixing an option action with an equity action is refused loudly (no order at all) --
  neither path can size both halves.
"""
import pytest
from unittest.mock import Mock, patch

from ba2_common.core.types import AnalysisUseCase, AssetClass, OrderRecommendation as Signal
from tests import factories
from tests.conftest import MockAccount
from tests.test_funded_entry_loop import _EnterExpert, _run_enter, _orders, FROZEN_NOW


def scenario(extra_actions=None):
    """A BUY-signal expert whose entry rule buys a call; HOLD admission OFF (the 8082 setup)."""
    acct = factories.create_account_definition(provider="MockAccount")
    rs = factories.create_ruleset(name="directional", subtype=AnalysisUseCase.ENTER_MARKET)
    actions = {"action_0": {
        "action_type": "buy_call", "strike_method": "percent_otm", "strike_param": 0,
        "dte_min": 20, "dte_max": 45, "sizing": 10.0,
        "min_open_interest": 100, "max_spread_pct": 30.}}
    actions.update(extra_actions or {})
    ea = factories.create_event_action(
        name="directional", subtype=AnalysisUseCase.ENTER_MARKET,
        triggers={"trigger_0": {"event_type": "rec_direction", "operator": ">", "value": 0}},
        actions=actions)
    factories.link_rule_to_ruleset(rs.id, ea.id, 0)
    inst = factories.create_expert_instance(account_id=acct.id, expert="_EnterExpert",
        virtual_equity_pct=100., enter_market_ruleset_id=rs.id)
    expert = _EnterExpert(inst.id)
    expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                         "evaluate_entry_rules_on_hold": (False, "bool")})
    factories.create_recommendation(instance_id=inst.id, symbol="AAPL",
        recommended_action=Signal.BUY, confidence=80., price_at_date=150.,
        expected_profit_percent=5., created_at=FROZEN_NOW)
    return inst, expert, MockAccount(acct.id)


def _transactions():
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import Transaction
    with get_db() as s:
        return list(s.exec(select(Transaction)).all())


def test_a_buy_signal_option_entry_bypasses_the_equity_risk_manager():
    inst, expert, account = scenario()
    manager = Mock()
    manager.size_candidate_orders.side_effect = AssertionError(
        "an option entry must never be sized by the equity risk manager")
    with patch("ba2_trade_platform.core.TradeRiskManagement.get_risk_management",
               return_value=manager):
        result = _run_enter(expert, account, inst.id)

    assert result, "the option entry must open"
    orders = _orders()
    assert orders and all(o.asset_class == AssetClass.OPTION for o in orders), (
        f"only option orders expected, got {[(o.asset_class, o.order_type) for o in orders]}")
    assert all(o.stop_price is None for o in orders), "no equity safeguard stop on an option entry"
    (txn,) = _transactions()
    entry = next(o for o in orders if o.depends_on_order is None)
    assert txn.quantity == entry.quantity, "the transaction keeps the contract quantity"


def test_a_rule_mixing_option_and_equity_actions_is_refused():
    inst, expert, account = scenario({"action_1": {"action_type": "buy"}})

    manager = Mock()
    manager.size_candidate_orders.side_effect = AssertionError("must be refused before sizing")
    with patch("ba2_trade_platform.core.TradeRiskManagement.get_risk_management",
               return_value=manager):
        assert not _run_enter(expert, account, inst.id)
    assert not _orders()
