"""Characterization test for the LIVE enter/order-creation path
(TradeManager.process_expert_recommendations_after_analysis).

There was NO automated coverage of this real-money path (the other TradeManager tests cover only
trigger/OCO/fill mechanics). This locks its observable behavior — which recommendations become
SUBMITTED orders, at what RM-sized quantity — so the planned temp-order-list rewrite of the enter
path (build candidates -> RM sizes in memory -> persist + submit only funded) can be validated as
behavior-preserving instead of shipped blind.

Wiring: a RecordingAccount captures every submit; get_expert_instance_from_id / get_account_class /
the instance resolver are pointed at the mock expert + recording account; _has_pending_analysis_jobs
is stubbed False. A simple enter ruleset (confidence>=60 & bullish -> buy) is seeded, plus three BUY
recommendations with different expected-profit so the RM's profit prioritization + per-instrument
cap produce a funded subset.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import (AnalysisUseCase, ExpertActionType, ExpertEventRuleType,
                                   OrderRecommendation, OrderStatus)

from tests.conftest import MockAccount
from tests import factories


class _RecordingAccount(MockAccount):
    """MockAccount that records submits + accepts the live submit_order(sl_price=...) signature."""

    def __init__(self, id_val):
        super().__init__(id_val)
        self.submitted = []  # list of (symbol, quantity, sl_price)
        self._balance = 100_000.0
        self._prices = {"AAPL": 100.0, "MSFT": 100.0, "GOOGL": 100.0}

    def submit_order(self, trading_order, tp_price=None, sl_price=None, is_closing_order=False):
        self.submitted.append((trading_order.symbol, trading_order.quantity, sl_price))
        trading_order.status = OrderStatus.NEW  # submitted; awaiting fill
        return trading_order

    def refresh_orders(self, fetch_all=False):
        return None

    def get_orders(self, status=None):
        return []

    def get_instrument_current_price(self, symbol_or_list, price_type="bid"):
        if isinstance(symbol_or_list, (list, tuple, set)):
            return {s: self._prices.get(s, 100.0) for s in symbol_or_list}
        return self._prices.get(symbol_or_list, 100.0)


class _EnterExpert(MarketExpertInterface):
    """Minimal classic-RM expert: inherits the base RM trading-setting definitions (enable_buy,
    allow_automated_trade_opening, max_virtual_equity_per_instrument_percent, ...)."""

    def __init__(self, id_val):
        self.id = id_val
        self._settings_cache = None

    @classmethod
    def description(cls):
        return "enter-path characterization expert"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


def _seed_enter_ruleset():
    """A minimal enter_market ruleset: bullish & confidence>=60 -> buy."""
    rs = factories.create_ruleset(name="enter-char", subtype=AnalysisUseCase.ENTER_MARKET)
    ea = factories.create_event_action(
        name="buy-tier", subtype=AnalysisUseCase.ENTER_MARKET,
        triggers={"trigger_0": {"event_type": "bullish"},
                  "trigger_1": {"event_type": "confidence", "operator": ">=", "value": 60.0}},
        actions={"action_0": {"action_type": ExpertActionType.BUY.value}},
    )
    factories.link_rule_to_ruleset(rs.id, ea.id, 0)
    return rs.id


def _resolver_for(expert, account):
    class _R:
        def get_expert_instance(self, expert_id):
            return expert
        def get_account_instance(self, account_id):
            return account
        def get_account_instance_from_transaction(self, transaction):
            return account
    return _R()


@pytest.mark.usefixtures("reset_test_db")
def test_live_enter_path_submits_rm_sized_funded_orders():
    from ba2_common.core.instance_resolver import (get_instance_resolver, set_instance_resolver)
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    acct_def = factories.create_account_definition(provider="MockAccount")
    ruleset_id = _seed_enter_ruleset()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_EnterExpert", virtual_equity_pct=100.0,
        enter_market_ruleset_id=ruleset_id)

    expert = _EnterExpert(inst.id)
    expert.save_settings({
        "allow_automated_trade_opening": (True, "bool"),
        "enable_buy": (True, "bool"),
        "enable_sell": (False, "bool"),
        "max_virtual_equity_per_instrument_percent": (40.0, "float"),
    })
    account = _RecordingAccount(acct_def.id)

    now = datetime.now(timezone.utc)
    for sym, ep in (("AAPL", 30.0), ("MSFT", 20.0), ("GOOGL", 10.0)):
        factories.create_recommendation(
            instance_id=inst.id, symbol=sym, recommended_action=OrderRecommendation.BUY,
            expected_profit_percent=ep, price_at_date=100.0, confidence=90.0, created_at=now)

    prev_resolver = None
    try:
        try:
            prev_resolver = get_instance_resolver()
        except Exception:  # noqa: BLE001
            prev_resolver = None
        set_instance_resolver(_resolver_for(expert, account))

        # These are imported LOCALLY inside the method, so patch them at their source modules.
        with patch("ba2_trade_platform.core.utils.get_expert_instance_from_id",
                   return_value=expert), \
             patch("ba2_trade_platform.modules.accounts.get_account_class",
                   return_value=(lambda _id: account)), \
             patch.object(type(get_trade_manager()), "_has_pending_analysis_jobs",
                          return_value=False):
            tm = get_trade_manager()
            created = tm.process_expert_recommendations_after_analysis(inst.id, lookback_days=7)

        # Behavior lock (baseline for the temp-list rewrite): the RM's profit-prioritized sizing
        # funds AAPL+MSFT to the 40% per-instrument cap (400 sh @ $100 = $40k each) and GOOGL with
        # the $20k remainder (200 sh), and all three are SUBMITTED. The temp-list rewrite must
        # reproduce this exact submitted set (size_candidate_orders == the DB path).
        submitted = {sym: qty for (sym, qty, _sl) in account.submitted}
        assert submitted == {"AAPL": 400.0, "MSFT": 400.0, "GOOGL": 200.0}, submitted
        assert all(qty and qty > 0 for (_s, qty, _sl) in account.submitted)
        assert created, "process_expert_recommendations_after_analysis should return created orders"

        # 2026-09-02: this run must also be VISIBLE in the Risk Manager Runs UI. Before this
        # fix, size_candidate_orders (this exact live path) never called into
        # risk_manager_run.record_run at all -- only the DB-pending-order path
        # (review_and_prioritize_pending_orders) did, and dev's real orders never go through
        # that path, leaving the table permanently empty despite the manager genuinely
        # sizing real orders every day.
        from ba2_common.core.db import get_all_instances
        from ba2_common.core.models import RiskManagerRun
        runs = [r for r in get_all_instances(RiskManagerRun) if r.expert_instance_id == inst.id]
        assert len(runs) == 1, "the live enter path must persist exactly one RiskManagerRun"
        run = runs[0]
        assert run.mode == "classic"
        assert run.symbols_received == 3
        assert run.symbols_funded == 3
        assert {d["symbol"] for d in run.decisions} == {"AAPL", "MSFT", "GOOGL"}
    finally:
        if prev_resolver is not None:
            set_instance_resolver(prev_resolver)
