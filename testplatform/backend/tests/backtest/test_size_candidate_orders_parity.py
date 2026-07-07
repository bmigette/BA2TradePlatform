"""Temp-order-list flow (P1e / P3 core): TradeRiskManagement.size_candidate_orders sizes an
IN-MEMORY candidate list and returns the funded subset with NO DB writes / deletes — the
replacement for the old "persist qty=0 orders -> RM sizes -> delete unfunded" churn.

This test proves size_candidate_orders produces the SAME funded quantities as the DB path
(review_and_prioritize_pending_orders) for identical inputs, so the enter path can switch to it
and persist only the funded orders.
"""
from datetime import datetime

from ba2_common.core.db import add_instance
from ba2_common.core.models import ExpertRecommendation, TradingOrder
from ba2_common.core.types import (OrderDirection, OrderRecommendation, OrderStatus, OrderType,
                                   RiskLevel, TimeHorizon)

_CFG = {"starting_cash": 100_000.0, "commission_per_trade": 0.0,
        "slippage_bps": 0.0, "fill_model": "next_bar_open"}
_D = datetime(2024, 1, 3)


def _bars(px):
    return [{"Date": datetime(2024, 1, d), "Open": px, "High": px, "Low": px,
             "Close": px, "Volume": 1000} for d in (2, 3, 4)]


def _rec(expert_id, symbol, ep):
    return add_instance(ExpertRecommendation(
        instance_id=expert_id, symbol=symbol, recommended_action=OrderRecommendation.BUY,
        expected_profit_percent=ep, price_at_date=100.0, confidence=90.0,
        risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.MEDIUM_TERM, created_at=_D))


def _build(expert_id=7701, account_id=7701):
    from app.services.backtest.backtest_db import (backtest_trading_db,
                                                   seed_account_definition, seed_expert_instance)
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"candsize-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, _CFG)
    ruleset_id = seed_enter_long_ruleset()
    seed_expert_instance(account_id=account_id, expert_class_name="FMPRating",
                         enter_market_ruleset_id=ruleset_id, instance_id=expert_id)
    ps = AsOfPriceSource(ohlcv_provider=None)
    for sym in ("AAPL", "MSFT", "NVDA"):
        ps.load_bars(sym, _bars(100.0))
    ps.set_clock(_D)
    account = BacktestAccount(account_id, ps, _CFG)
    resolver.register_account(account_id, account)

    # a real expert instance so has_available_balance + settings resolve (enable_buy on, small
    # per-instrument cap so not everything is fundable -> exercises the funded/unfunded split)
    from ba2_experts import FMPRating  # a classic-RM expert
    expert = FMPRating(expert_id)
    expert.save_settings({
        "allow_automated_trade_opening": (True, "bool"), "enable_buy": (True, "bool"),
        "enable_sell": (False, "bool"),
        "max_virtual_equity_per_instrument_percent": (40.0, "float"),
    })
    resolver.register_expert(expert_id, expert)
    return account, expert_id, ps, ctx


def _transient_order(account_id, symbol, rec_id):
    return TradingOrder(account_id=account_id, symbol=symbol, quantity=0.0,
                        side=OrderDirection.BUY, order_type=OrderType.MARKET,
                        status=OrderStatus.PENDING, expert_recommendation_id=rec_id)


def test_candidate_sizing_matches_db_path_funded_quantities():
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement
    from ba2_common.core.db import get_instance

    account, expert_id, ps, ctx = _build()
    try:
        recs = {"AAPL": _rec(expert_id, "AAPL", 30.0),
                "MSFT": _rec(expert_id, "MSFT", 20.0),
                "NVDA": _rec(expert_id, "NVDA", 10.0)}

        # --- in-memory candidate path (no DB writes) ---
        candidates = [(_transient_order(account.id, s, rid), get_instance(ExpertRecommendation, rid))
                      for s, rid in recs.items()]
        funded_cand = TradeRiskManagement().size_candidate_orders(expert_id, candidates)
        cand_qty = {o.symbol: o.quantity for o in funded_cand}

        # --- DB path (persist qty=0 orders, RM sizes + deletes unfunded) ---
        for s, rid in recs.items():
            add_instance(_transient_order(account.id, s, rid))  # persisted this time
        funded_db = TradeRiskManagement().review_and_prioritize_pending_orders(expert_id)
        db_qty = {o.symbol: o.quantity for o in funded_db if o.quantity and o.quantity > 0}

        assert cand_qty == db_qty, f"candidate {cand_qty} != db {db_qty}"
        assert cand_qty, "expected at least one funded order"
        # sizing math is deterministic + identical -> at least one symbol funded the same both ways
    finally:
        ctx.__exit__(None, None, None)


def test_candidate_sizing_no_db_writes():
    from sqlmodel import Session, select
    from ba2_common.core.db import get_db
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement

    account, expert_id, ps, ctx = _build()
    try:
        rid = _rec(expert_id, "AAPL", 30.0)
        from ba2_common.core.db import get_instance
        candidates = [(_transient_order(account.id, "AAPL", rid),
                       get_instance(ExpertRecommendation, rid))]
        funded = TradeRiskManagement().size_candidate_orders(expert_id, candidates)
        assert funded and funded[0].quantity > 0
        # NO TradingOrder rows were persisted by the candidate path
        with Session(get_db().bind) as s:
            assert s.exec(select(TradingOrder)).all() == []
    finally:
        ctx.__exit__(None, None, None)
