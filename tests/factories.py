"""
Factory functions for creating test model instances with sensible defaults.

All factories accept **kwargs to override any field. Instances are persisted
via add_instance (uses the patched test engine) and returned with DB-assigned IDs.
"""
from datetime import datetime, timezone

from ba2_trade_platform.core.models import (
    AccountDefinition, ExpertInstance, ExpertRecommendation,
    TradingOrder, Transaction, Ruleset, EventAction,
    RulesetEventActionLink, AppSetting, MarketAnalysis,
)
from ba2_trade_platform.core.types import (
    OrderDirection, OrderType, OrderStatus, OrderOpenType,
    OrderRecommendation, RiskLevel, TimeHorizon, TransactionStatus,
    ExpertEventRuleType, AnalysisUseCase, MarketAnalysisStatus,
)
from ba2_trade_platform.core.db import add_instance


def _create_and_persist(obj):
    """Add instance to DB using expunge_after_flush to avoid DetachedInstanceError."""
    add_instance(obj, expunge_after_flush=True)
    return obj


def create_account_definition(
    name="Test Account", provider="MockAccount", description="Test account",
    **kwargs
):
    obj = AccountDefinition(name=name, provider=provider, description=description, **kwargs)
    return _create_and_persist(obj)


def create_expert_instance(
    account_id, expert="MockExpert", enabled=True, virtual_equity_pct=100.0,
    **kwargs
):
    obj = ExpertInstance(
        account_id=account_id, expert=expert, enabled=enabled,
        virtual_equity_pct=virtual_equity_pct, **kwargs
    )
    return _create_and_persist(obj)


def create_recommendation(
    instance_id, symbol="AAPL",
    recommended_action=OrderRecommendation.BUY,
    expected_profit_percent=5.0, price_at_date=150.0,
    confidence=75.0, risk_level=RiskLevel.MEDIUM,
    time_horizon=TimeHorizon.SHORT_TERM,
    details="Test recommendation",
    **kwargs
):
    obj = ExpertRecommendation(
        instance_id=instance_id,
        symbol=symbol,
        recommended_action=recommended_action,
        expected_profit_percent=expected_profit_percent,
        price_at_date=price_at_date,
        confidence=confidence,
        risk_level=risk_level,
        time_horizon=time_horizon,
        details=details,
        **kwargs,
    )
    return _create_and_persist(obj)


def create_transaction(
    symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
    status=TransactionStatus.OPENED, open_price=150.0,
    expert_id=None, **kwargs
):
    obj = Transaction(
        symbol=symbol, quantity=quantity, side=side,
        status=status, open_price=open_price,
        open_date=datetime.now(timezone.utc),
        expert_id=expert_id,
        **kwargs,
    )
    return _create_and_persist(obj)


def create_trading_order(
    account_id, symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
    order_type=OrderType.MARKET, status=OrderStatus.PENDING,
    transaction_id=None, **kwargs
):
    obj = TradingOrder(
        account_id=account_id,
        symbol=symbol,
        quantity=quantity,
        side=side,
        order_type=order_type,
        status=status,
        transaction_id=transaction_id,
        **kwargs,
    )
    return _create_and_persist(obj)


def create_ruleset(
    name="Test Ruleset", description="Test ruleset",
    type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
    subtype=None, **kwargs
):
    obj = Ruleset(name=name, description=description, type=type, subtype=subtype, **kwargs)
    return _create_and_persist(obj)


def create_event_action(
    name="Test Rule",
    type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
    triggers=None, actions=None, continue_processing=False,
    **kwargs
):
    obj = EventAction(
        name=name,
        type=type,
        triggers=triggers or {},
        actions=actions or {},
        continue_processing=continue_processing,
        **kwargs,
    )
    return _create_and_persist(obj)


def create_market_analysis(
    symbol="AAPL", expert_instance_id=1,
    status=MarketAnalysisStatus.PENDING,
    subtype=AnalysisUseCase.ENTER_MARKET,
    **kwargs
):
    obj = MarketAnalysis(
        symbol=symbol,
        expert_instance_id=expert_instance_id,
        status=status,
        subtype=subtype,
        **kwargs,
    )
    return _create_and_persist(obj)


def link_rule_to_ruleset(ruleset_id, eventaction_id, order_index=0):
    """Create a RulesetEventActionLink record (composite PK, no id field)."""
    from ba2_trade_platform.core.db import get_db
    obj = RulesetEventActionLink(
        ruleset_id=ruleset_id,
        eventaction_id=eventaction_id,
        order_index=order_index,
    )
    with get_db() as session:
        session.add(obj)
        session.commit()
    return obj
