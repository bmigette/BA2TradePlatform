"""
Shared test fixtures for BA2 Trade Platform unit tests.

Provides:
- In-memory SQLite test database (patched over production engine)
- MockAccount: concrete AccountInterface with canned broker responses
- MockExpert: concrete MarketExpertInterface with canned analysis results
- Factory-created DB records (account definitions, expert instances, etc.)
"""
import pytest
from unittest.mock import patch, MagicMock
from sqlmodel import SQLModel, Session, create_engine
from datetime import datetime, timezone

# Import models to register them with SQLModel metadata
from ba2_trade_platform.core.models import (
    AccountDefinition, ExpertInstance, ExpertSetting, AccountSetting,
    ExpertRecommendation, MarketAnalysis, TradingOrder, Transaction,
    Ruleset, EventAction, RulesetEventActionLink, AppSetting,
    TradeActionResult, ActivityLog, Instrument, Position,
    AnalysisOutput, PersistedQueueTask, LLMUsageLog, SmartRiskManagerJob,
)
from ba2_trade_platform.core.types import (
    OrderStatus, OrderDirection, OrderType, OrderOpenType,
    OrderRecommendation, RiskLevel, TimeHorizon, TransactionStatus,
    ExpertEventRuleType, AnalysisUseCase, MarketAnalysisStatus,
)
from ba2_trade_platform.core.interfaces.AccountInterface import AccountInterface
from ba2_trade_platform.core.interfaces.MarketExpertInterface import MarketExpertInterface


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def test_engine():
    """Create an in-memory SQLite engine shared across the entire test session."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(autouse=True)
def patch_db_engine(test_engine):
    """Monkeypatch the production db.engine so all code uses the test DB."""
    with patch("ba2_trade_platform.core.db.engine", test_engine):
        yield


@pytest.fixture
def db_session(test_engine):
    """Provide a fresh session per test with automatic cleanup."""
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


# ---------------------------------------------------------------------------
# MockAccount — concrete AccountInterface for tests
# ---------------------------------------------------------------------------

class MockAccount(AccountInterface):
    """Test double for AccountInterface with configurable canned responses."""

    def __init__(self, id_or_definition):
        """Initialize with an ID. Bypasses parent __init__ to avoid DB lookups."""
        if isinstance(id_or_definition, int):
            self.id = id_or_definition
        else:
            self.id = id_or_definition
        self._settings_cache = None
        # Instance-level state to avoid cross-test leaks
        self._prices = {"AAPL": 150.0, "MSFT": 400.0, "GOOGL": 170.0}
        self._balance = 100_000.0
        self._positions = []
        self._submit_order_result = True
        # Initialize price cache for this account
        with self._CACHE_LOCK:
            if self.id not in self._GLOBAL_PRICE_CACHE:
                self._GLOBAL_PRICE_CACHE[self.id] = {}

    @classmethod
    def get_settings_definitions(cls):
        return {}

    def get_balance(self):
        return self._balance

    def get_account_info(self):
        return {"balance": self._balance, "equity": self._balance}

    def get_positions(self):
        return self._positions

    def get_orders(self, status=None):
        return []

    def get_instrument_current_price(self, symbol):
        return self._prices.get(symbol)

    def submit_order(self, order, is_closing_order=False):
        if not self._submit_order_result:
            return None
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.open_price = self._prices.get(order.symbol, 100.0)
        return order

    def cancel_order(self, order):
        order.status = OrderStatus.CANCELED
        return order

    def refresh_account(self):
        pass

    def refresh_orders(self):
        pass

    def refresh_positions(self):
        pass

    def get_order_by_broker_id(self, broker_order_id):
        return None


# ---------------------------------------------------------------------------
# MockExpert — concrete MarketExpertInterface for tests
# ---------------------------------------------------------------------------

class MockExpert(MarketExpertInterface):
    """Test double for MarketExpertInterface with canned analysis results."""

    def __init__(self, id_val):
        """Initialize with an ID. Bypasses parent __init__ to avoid DB lookups."""
        self.id = id_val
        self._settings_cache = None

    @classmethod
    def get_settings_definitions(cls):
        return {
            "test_setting": {
                "type": "str",
                "required": False,
                "default": "test_value",
                "description": "A test setting",
            }
        }

    @classmethod
    def description(cls):
        return "Mock expert for testing"

    def run_analysis(self, symbol, market_analysis):
        return ExpertRecommendation(
            instance_id=self.id,
            market_analysis_id=market_analysis.id if market_analysis else None,
            symbol=symbol,
            recommended_action=OrderRecommendation.BUY,
            expected_profit_percent=5.0,
            price_at_date=150.0,
            details="Mock analysis result",
            confidence=75.0,
            risk_level=RiskLevel.MEDIUM,
            time_horizon=TimeHorizon.SHORT_TERM,
        )

    def get_enabled_instruments(self):
        return ["AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# Convenience fixtures for common test objects
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_account_def():
    """Create and persist an AccountDefinition, return it with its ID."""
    from tests.factories import create_account_definition
    return create_account_definition()


@pytest.fixture
def mock_account(mock_account_def):
    """Return a MockAccount instance backed by a real DB record."""
    return MockAccount(mock_account_def.id)


@pytest.fixture
def mock_expert_instance(mock_account_def):
    """Create and persist an ExpertInstance, return it."""
    from tests.factories import create_expert_instance
    return create_expert_instance(account_id=mock_account_def.id, expert="MockExpert")


@pytest.fixture
def mock_expert(mock_expert_instance):
    """Return a MockExpert instance backed by a real DB record."""
    return MockExpert(mock_expert_instance.id)


@pytest.fixture
def sample_recommendation(mock_expert_instance):
    """Create and persist a sample ExpertRecommendation."""
    from tests.factories import create_recommendation
    return create_recommendation(instance_id=mock_expert_instance.id)
