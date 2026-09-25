"""LIVE: an enter rule whose BUY carries ``lot_size`` is sized and placed in whole lots.

The live half of the O_CC / O_PP round-lot fix (the backtest half is
``testplatform/backend/tests/backtest/test_equity_lot_size_engine.py``). The funded-entry loop in
``TradeManager.process_expert_recommendations_after_analysis`` builds the RM candidate through
``trade_cycle.build_entry_candidate``, which used to read ``data`` off the RECOMMENDATION only --
so a live rule that DID carry ``lot_size: 100`` on its buy was sized as if it had none, and the
broker got 536 shares.

Reuses the real funded-entry harness of ``tests/test_funded_entry_loop.py`` (file-backed DB, the
real ``AccountInterface.submit_order``, only the broker call doubled).
"""
from __future__ import annotations

from unittest.mock import patch

from ba2_common.core.types import (AnalysisUseCase, ExpertActionType, OrderRecommendation,
                                   OrderType)

from tests import factories
from tests.test_funded_entry_loop import (  # noqa: F401  (file_db is a fixture)
    FROZEN_NOW, _EnterExpert, _FundedEntryAccount, _orders, _run_enter, file_db)

#: 40% of $100k = $40,000 per instrument; / $74.50 = 536.9 -> 536 shares: an ODD lot.
ODD_LOT_PRICE = 74.50
#: $40,000 / $450 = 88 shares: less than one lot.
SUB_LOT_PRICE = 450.0


def _scenario(*, lot_size, sizing_mode=None):
    acct_def = factories.create_account_definition(provider="MockAccount")
    rs = factories.create_ruleset(name="enter-lot", subtype=AnalysisUseCase.ENTER_MARKET)
    buy = {"action_type": ExpertActionType.BUY.value}
    if lot_size is not None:
        buy["lot_size"] = lot_size
    ea = factories.create_event_action(
        name="buy-lot", subtype=AnalysisUseCase.ENTER_MARKET,
        triggers={"trigger_0": {"event_type": "bullish"}},
        actions={"action_0": buy})
    factories.link_rule_to_ruleset(rs.id, ea.id, 0)
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_EnterExpert", virtual_equity_pct=100.0,
        enter_market_ruleset_id=rs.id)
    expert = _EnterExpert(inst.id)
    expert.save_settings({
        "allow_automated_trade_opening": (True, "bool"),
        "enable_buy": (True, "bool"),
        "enable_sell": (False, "bool"),
        "max_virtual_equity_per_instrument_percent": (40.0, "float"),
        **({"sizing_mode": (sizing_mode, "str")} if sizing_mode else {}),
    })
    factories.create_recommendation(
        instance_id=inst.id, symbol="AAPL", recommended_action=OrderRecommendation.BUY,
        expected_profit_percent=30.0, price_at_date=100.0, confidence=90.0,
        created_at=FROZEN_NOW)
    return acct_def, inst, expert


def _run(file_db, price, *, lot_size, sizing_mode=None):
    """Drive the live loop; return (candidate data the RM saw, the broker submits)."""
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement

    acct, inst, expert = _scenario(lot_size=lot_size, sizing_mode=sizing_mode)
    account = _FundedEntryAccount(acct.id, probe=file_db)
    account._prices["AAPL"] = price

    seen = []
    real = TradeRiskManagement.size_candidate_orders

    def _spy(self, expert_instance_id, candidates):
        seen.extend(dict(c.data) if c.data else c.data for c, _rec in candidates)
        return real(self, expert_instance_id, candidates)

    with patch.object(TradeRiskManagement, "size_candidate_orders", _spy):
        _run_enter(expert, account, inst.id)
    return seen, account.submits


def test_the_live_candidate_carries_the_rules_lot_size_and_the_broker_gets_whole_lots(file_db):
    seen, submits = _run(file_db, ODD_LOT_PRICE, lot_size=100)
    assert seen and all((d or {}).get("lot_size") == 100 for d in seen), (
        f"the RM candidate did not carry the fired BuyAction's lot_size: {seen}")
    assert [s["quantity"] for s in submits] == [500], (
        f"the broker must receive 536 floored to whole lots (500), got "
        f"{[s['quantity'] for s in submits]}")
    entry = [o for o in _orders("AAPL") if o.order_type == OrderType.MARKET]
    assert entry and entry[0].quantity == 500


def test_less_than_one_lot_is_not_placed_at_all(file_db):
    """...and the refusal is said at WARNING, naming the symbol (as the backtest test pins)."""
    import logging
    from ba2_common.logger import logger as ba2_logger

    class _Grab(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.WARNING)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    grab = _Grab()
    ba2_logger.addHandler(grab)
    try:
        _seen, submits = _run(file_db, SUB_LOT_PRICE, lot_size=100)
    finally:
        ba2_logger.removeHandler(grab)
    assert any("Lot sizing REFUSED AAPL" in m and "less than one lot of 100" in m
               for m in grab.messages), (
        f"the sub-lot refusal was not announced at WARNING: {grab.messages[-5:]}")
    assert submits == [], (
        f"88 affordable shares is less than one lot; nothing may reach the broker: {submits}")
    assert [o for o in _orders("AAPL") if o.order_type == OrderType.MARKET] == [], (
        "an unfunded candidate is never persisted (temp-order-list flow)")


def test_a_rule_without_lot_size_is_sized_exactly_as_before(file_db):
    seen, submits = _run(file_db, ODD_LOT_PRICE, lot_size=None)
    assert all(not (d or {}).get("lot_size") for d in seen), seen
    assert [s["quantity"] for s in submits] == [536]


def test_risk_based_sizing_also_floors_to_whole_lots(file_db):
    """The risk_atr sizer reads the same candidate ``data``; its result is a whole lot too."""
    _seen, control = _run(file_db, ODD_LOT_PRICE, lot_size=None, sizing_mode="risk_atr")
    assert control and control[0]["quantity"] % 100 != 0, (
        f"fixture precondition: risk_atr alone must size an odd lot here, got {control}")
    _seen, submits = _run(file_db, ODD_LOT_PRICE, lot_size=100, sizing_mode="risk_atr")
    assert [s["quantity"] for s in submits] == [control[0]["quantity"] // 100 * 100]
