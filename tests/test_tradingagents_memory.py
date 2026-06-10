"""Tests for FinancialSituationMemory's configurable memory injection scope."""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.memory import (
    FinancialSituationMemory,
)
from ba2_trade_platform.core.types import (
    OrderRecommendation, MarketAnalysisStatus, AnalysisUseCase,
    OrderDirection, TransactionStatus, OrderStatus, OrderType,
)
from tests.factories import (
    create_account_definition, create_expert_instance, create_market_analysis,
    create_recommendation, create_transaction, create_trading_order,
)


@pytest.fixture
def expert_instance(db_session):
    account = create_account_definition()
    return create_expert_instance(account.id, expert="TradingAgents")


def _make_past_analysis(expert_instance_id, symbol, days_ago, action=OrderRecommendation.SELL):
    ma = create_market_analysis(
        symbol=symbol,
        expert_instance_id=expert_instance_id,
        status=MarketAnalysisStatus.COMPLETED,
        subtype=AnalysisUseCase.ENTER_MARKET,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    create_recommendation(
        instance_id=expert_instance_id,
        market_analysis_id=ma.id,
        symbol=symbol,
        recommended_action=action,
    )
    return ma


class TestScopeNone:
    def test_none_scope_returns_empty(self, expert_instance):
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=1)
        mem = FinancialSituationMemory(
            "bull_memory",
            {"memory_injection_scope": "none", "memory_max_trades": 2, "memory_lookback_days": 14},
            symbol="AAPL",
            market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        assert mem.get_memories("any situation") == []


class TestScopeSameSymbol:
    def test_returns_only_same_symbol_within_lookback(self, expert_instance):
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=1)
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=20)  # outside 14-day window
        mem = FinancialSituationMemory(
            "bull_memory",
            {"memory_injection_scope": "same_symbol", "memory_max_trades": 2, "memory_lookback_days": 14},
            symbol="AAPL",
            market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        memories = mem.get_memories("any situation")
        assert len(memories) == 1

    def test_respects_max_trades_limit(self, expert_instance):
        for i in range(5):
            _make_past_analysis(expert_instance.id, "AAPL", days_ago=i + 1)
        mem = FinancialSituationMemory(
            "bull_memory",
            {"memory_injection_scope": "same_symbol", "memory_max_trades": 2, "memory_lookback_days": 14},
            symbol="AAPL",
            market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        memories = mem.get_memories("any situation")
        assert len(memories) == 2

    def test_does_not_include_cross_ticker_summary(self, expert_instance):
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=1)
        # Closed losing trade on a DIFFERENT symbol, recent
        create_transaction(
            symbol="TOST", side=OrderDirection.BUY, status=TransactionStatus.CLOSED,
            open_price=100.0, close_price=80.0, quantity=10,
            expert_id=expert_instance.id,
        )
        mem = FinancialSituationMemory(
            "bull_memory",
            {"memory_injection_scope": "same_symbol", "memory_max_trades": 2, "memory_lookback_days": 14},
            symbol="AAPL",
            market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        memories = mem.get_memories("any situation")
        combined = "\n".join(m["recommendation"] for m in memories)
        assert "TOST" not in combined


class TestScopeAllSymbols:
    def test_includes_cross_ticker_summary_within_lookback(self, expert_instance):
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=1)
        txn = create_transaction(
            symbol="TOST", side=OrderDirection.BUY, status=TransactionStatus.CLOSED,
            open_price=100.0, close_price=80.0, quantity=10,
            expert_id=expert_instance.id,
        )
        txn.close_date = datetime.now(timezone.utc) - timedelta(days=2)
        from ba2_trade_platform.core.db import update_instance
        update_instance(txn)

        mem = FinancialSituationMemory(
            "bull_memory",
            {"memory_injection_scope": "all_symbols", "memory_max_trades": 2, "memory_lookback_days": 14},
            symbol="AAPL",
            market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        memories = mem.get_memories("any situation")
        combined = "\n".join(m["recommendation"] for m in memories)
        assert "TOST" in combined

    def test_excludes_cross_ticker_outside_lookback(self, expert_instance):
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=1)
        txn = create_transaction(
            symbol="TOST", side=OrderDirection.BUY, status=TransactionStatus.CLOSED,
            open_price=100.0, close_price=80.0, quantity=10,
            expert_id=expert_instance.id,
        )
        txn.close_date = datetime.now(timezone.utc) - timedelta(days=20)  # outside 14-day window
        from ba2_trade_platform.core.db import update_instance
        update_instance(txn)

        mem = FinancialSituationMemory(
            "bull_memory",
            {"memory_injection_scope": "all_symbols", "memory_max_trades": 2, "memory_lookback_days": 14},
            symbol="AAPL",
            market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        memories = mem.get_memories("any situation")
        combined = "\n".join(m["recommendation"] for m in memories)
        assert "TOST" not in combined


class TestDefaults:
    def test_missing_config_keys_use_defaults(self, expert_instance):
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=1)
        mem = FinancialSituationMemory(
            "bull_memory", {}, symbol="AAPL", market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        assert mem.scope == "same_symbol"
        assert mem.max_trades == 2
        assert mem.lookback_days == 14
