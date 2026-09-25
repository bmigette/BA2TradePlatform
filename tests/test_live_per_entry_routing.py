"""LIVE half of E1: the same two-rule ruleset routes each entry the way the backtest does.

Plan 2026-09-25 option-stage2-build, revision Task E1. The backtest engine's per-entry routing
is pinned in ``testplatform/backend/tests/backtest/test_per_entry_routing.py``; this is the
same shape driven through the REAL live ``TradeManager.process_expert_recommendations_after_
analysis``: ONE enter ruleset with an option rule (confident recommendations -> ``buy_call``)
and an equity rule (lukewarm -> ``buy``), two recommendations on two symbols, ONE pass.

``ROUTING`` below is the table both files assert, and both runtimes decide it through the SAME
function (``trade_cycle.entry_route``) -- spied here so the test proves the live loop actually
calls it rather than re-deriving the answer, which is how the two runtimes drifted before
(f73a33f3, 8082 2026-09-24).

Also pinned: the equity member's ``entry_tag`` reaches the live entry order through the
RM-funded flow (O_CC / O_PP enter with a stock buy; their tag-scoped exits read that row).
"""
from __future__ import annotations

from unittest.mock import Mock, patch

from ba2_common.core.types import AnalysisUseCase, AssetClass, OrderRecommendation

from tests import factories
from tests.test_funded_entry_loop import (FROZEN_NOW, _EnterExpert, _FundedEntryAccount,
                                          _orders, _run_enter, file_db)  # noqa: F401

#: symbol -> the route the SHARED router must choose for its fired entry (same table as the
#: backtest test).
ROUTING = {"AAPL": "option", "MSFT": "equity"}
CONFIDENCE = {"AAPL": 90.0, "MSFT": 60.0}
TAG_OPTION, TAG_EQUITY = 1, 21


def _scenario():
    acct = factories.create_account_definition(provider="MockAccount")
    rs = factories.create_ruleset(name="per-entry", subtype=AnalysisUseCase.ENTER_MARKET)
    option_rule = factories.create_event_action(
        name="option-member", subtype=AnalysisUseCase.ENTER_MARKET,
        triggers={"t0": {"event_type": "confidence", "operator": ">=", "value": 80.0}},
        actions={"a0": {"action_type": "buy_call", "strike_method": "percent_otm",
                        "strike_param": 0, "dte_min": 20, "dte_max": 45, "sizing": 10.0,
                        "min_open_interest": 100, "max_spread_pct": 30., "entry_tag": TAG_OPTION}})
    equity_rule = factories.create_event_action(
        name="equity-member", subtype=AnalysisUseCase.ENTER_MARKET,
        triggers={"t0": {"event_type": "confidence", "operator": "<", "value": 80.0}},
        actions={"a0": {"action_type": "buy", "entry_tag": TAG_EQUITY}})
    factories.link_rule_to_ruleset(rs.id, option_rule.id, 0)
    factories.link_rule_to_ruleset(rs.id, equity_rule.id, 1)
    inst = factories.create_expert_instance(
        account_id=acct.id, expert="_EnterExpert", virtual_equity_pct=100.0,
        enter_market_ruleset_id=rs.id)
    expert = _EnterExpert(inst.id)
    expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                          "enable_buy": (True, "bool"), "enable_sell": (False, "bool"),
                          "evaluate_entry_rules_on_hold": (False, "bool"),
                          "max_virtual_equity_per_instrument_percent": (40.0, "float")})
    for sym, conf in CONFIDENCE.items():
        factories.create_recommendation(
            instance_id=inst.id, symbol=sym, recommended_action=OrderRecommendation.BUY,
            confidence=conf, price_at_date=100.0, expected_profit_percent=10.0,
            created_at=FROZEN_NOW)
    return acct, inst, expert


def test_live_routes_each_entry_through_the_shared_router(file_db):
    from ba2_common.core import trade_cycle

    acct, inst, expert = _scenario()
    account = _FundedEntryAccount(acct.id, probe=file_db)

    decisions = []
    real_route = trade_cycle.entry_route

    def spy(action_types):
        route = real_route(action_types)
        decisions.append(route)
        return route

    sized = []
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement
    real_size = TradeRiskManagement.size_candidate_orders

    def size_spy(self, expert_id, candidates):
        sized.extend(c.symbol for c, _rec in candidates)
        return real_size(self, expert_id, candidates)

    with patch.object(trade_cycle, "entry_route", spy), \
         patch.object(TradeRiskManagement, "size_candidate_orders", size_spy):
        _run_enter(expert, account, inst.id)

    assert sorted(decisions) == sorted(ROUTING.values()), (
        f"the live loop did not route through trade_cycle.entry_route: {decisions}")

    aapl = _orders("AAPL")
    assert aapl and all(o.asset_class == AssetClass.OPTION for o in aapl), (
        f"the option member's entry must self-size as options: "
        f"{[(o.asset_class, o.order_type) for o in aapl]}")
    assert "AAPL" not in sized, "an option entry reached the equity RM"

    assert sized == ["MSFT"], f"only the equity entry is RM-sized, got {sized}"
    msft = [o for o in _orders("MSFT") if o.depends_on_order is None]
    assert msft and msft[0].asset_class != AssetClass.OPTION
    assert (msft[0].data or {}).get("entry_tag") == TAG_EQUITY, (
        f"the equity member's tag must ride its live entry order: {msft[0].data}")
