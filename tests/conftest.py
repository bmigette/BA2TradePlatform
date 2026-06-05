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
    OptionIVSnapshot, OptionActivity,
)
from ba2_trade_platform.core.types import (
    OrderStatus, OrderDirection, OrderType, OrderOpenType,
    OrderRecommendation, RiskLevel, TimeHorizon, TransactionStatus,
    ExpertEventRuleType, AnalysisUseCase, MarketAnalysisStatus,
)
from ba2_trade_platform.core.interfaces.AccountInterface import AccountInterface
from ba2_trade_platform.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
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

class MockAccount(AccountInterface, OptionsAccountInterface):
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
        self._option_positions = []          # list[OptionPosition]
        self._submitted_option_orders = []   # capture for assertions
        self._atm_iv = {"AAPL": 0.30, "MSFT": 0.28, "GOOGL": 0.33}
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

    def symbols_exist(self, symbols):
        return {s: True for s in symbols}

    def _submit_order_impl(self, trading_order, tp_price=None, sl_price=None, is_closing_order=False):
        if not self._submit_order_result:
            return None
        trading_order.status = OrderStatus.FILLED
        trading_order.filled_qty = trading_order.quantity
        trading_order.open_price = self._prices.get(trading_order.symbol, 100.0)
        return trading_order

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
        if isinstance(symbol_or_symbols, list):
            return {s: self._prices.get(s) for s in symbol_or_symbols}
        return self._prices.get(symbol_or_symbols)

    def get_order(self, order_id):
        return None

    def modify_order(self, order_id):
        return None

    def _set_order_tp_impl(self, trading_order, tp_price):
        return True

    def _set_order_sl_impl(self, trading_order, sl_price):
        return True

    def _set_order_tp_sl_impl(self, trading_order, tp_price, sl_price):
        return True

    def adjust_tp(self, transaction, new_tp_price, source=""):
        return True

    def adjust_sl(self, transaction, new_sl_price, source=""):
        return True

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        return True

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_balance_history(self, start_date=None, end_date=None):
        return []

    def is_drip_enabled(self):
        return False

    # --- OptionsAccountInterface (canned doubles) ---
    def _mk_contract(self, underlying, right, strike, expiry):
        from ba2_trade_platform.core.option_types import OptionContract
        spot = self._prices.get(underlying, 100.0)
        intrinsic = max(0.0, (spot - strike) if right.value == "call" else (strike - spot))
        mid = round(intrinsic + 2.0, 2)
        occ = f"{underlying}{expiry:%y%m%d}{'C' if right.value == 'call' else 'P'}{int(strike * 1000):08d}"
        return OptionContract(
            symbol=occ, underlying=underlying, option_type=right, strike=strike, expiry=expiry,
            bid=mid - 0.1, ask=mid + 0.1, last=mid, implied_volatility=0.30,
            delta=0.5, gamma=0.02, theta=-0.03, vega=0.1, open_interest=1000, volume=250)

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type=None,
                         strike_min=None, strike_max=None):
        from ba2_trade_platform.core.types import OptionRight
        # NOTE: expiry window not modeled; every contract uses expiry_max.
        expiry = expiry_max
        spot = self._prices.get(underlying, 100.0)
        rights = [option_type] if option_type else [OptionRight.CALL, OptionRight.PUT]
        out = []
        for r in rights:
            for k in (round(spot * 0.95), round(spot), round(spot * 1.05)):
                if strike_min is not None and k < strike_min:
                    continue
                if strike_max is not None and k > strike_max:
                    continue
                out.append(self._mk_contract(underlying, r, float(k), expiry))
        return out

    def get_option_quote(self, contract_symbol):
        from ba2_trade_platform.core.option_types import OptionQuote
        # NOTE: canned quote; intentionally not derived from _mk_contract pricing.
        return OptionQuote(symbol=contract_symbol, bid=2.0, ask=2.2, last=2.1,
                           implied_volatility=0.30, delta=0.5, gamma=0.02, theta=-0.03, vega=0.1)

    def get_atm_implied_volatility(self, underlying):
        return self._atm_iv.get(underlying, 0.30)

    def get_option_positions(self):
        return self._option_positions

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        from ba2_trade_platform.core.types import OrderStatus
        from ba2_trade_platform.core.db import update_instance
        trading_order.status = OrderStatus.FILLED
        trading_order.filled_qty = trading_order.quantity
        trading_order.broker_order_id = f"mock-opt-{trading_order.id}"
        update_instance(trading_order)
        if leg_orders:
            for i, lo in enumerate(leg_orders):
                lo.status = OrderStatus.FILLED
                lo.filled_qty = lo.quantity
                lo.broker_order_id = f"mock-opt-{trading_order.id}-leg{i}"
                update_instance(lo)
        self._submitted_option_orders.append(trading_order)
        return trading_order

    def close_option_position(self, position, order_type="limit", limit_price=None):
        from ba2_trade_platform.core.option_types import OptionLeg
        from ba2_trade_platform.core.types import OrderDirection
        close_side = OrderDirection.SELL if position.side == OrderDirection.BUY else OrderDirection.BUY
        intent = "sell_to_close" if position.side == OrderDirection.BUY else "buy_to_close"
        leg = OptionLeg(contract_symbol=position.contract_symbol, side=close_side,
                        position_intent=intent, option_type=position.option_type,
                        strike=position.strike, expiry=position.expiry, underlying=position.underlying)
        return self.submit_option_order([leg], int(position.quantity), order_type, limit_price,
                                        option_strategy="close")


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
