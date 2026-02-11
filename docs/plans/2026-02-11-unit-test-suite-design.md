# Unit Test Suite Design

## Goal

Create a comprehensive pytest-based unit test suite for BA2 Trade Platform with:
- Dedicated in-memory SQLite database per test session (no production DB)
- Mocked LLM API calls (OpenAI, etc.)
- Mocked broker API calls (Alpaca, IBKR)
- Mocked market data APIs (FMP, Finnhub, Alpha Vantage, yfinance)
- Full coverage of core logic, experts, accounts, and integration flows

## Directory Structure

```
tests/
├── conftest.py                    # Shared fixtures: test DB, mock account/expert, factories
├── factories.py                   # Factory functions to create test model instances
├── test_db.py                     # db.py CRUD, retry logic, activity logging
├── test_models.py                 # Model methods (get_current_open_qty, etc.)
├── test_types.py                  # Enum helpers and status group methods
├── test_trade_conditions.py       # All TradeCondition subclasses
├── test_trade_actions.py          # All TradeAction subclasses
├── test_trade_evaluator.py        # TradeActionEvaluator ruleset evaluation
├── test_trade_manager.py          # TradeManager order placement & recommendation handling
├── test_utils.py                  # core/utils.py helper functions
├── test_settings.py               # ExtendableSettingsInterface and settings CRUD
├── test_worker_queue.py           # WorkerQueue and AnalysisTask
├── test_smart_priority_queue.py   # SmartPriorityQueue round-robin logic
├── test_rules_export_import.py    # Ruleset export/import
├── test_trade_risk.py             # TradeRiskManagement
├── test_transaction_helper.py     # TransactionHelper
├── test_experts/
│   ├── __init__.py
│   ├── test_fmp_rating.py         # FMPRating expert
│   ├── test_fmp_senate_copy.py    # FMPSenateTraderCopy
│   ├── test_fmp_senate_weight.py  # FMPSenateTraderWeight
│   ├── test_finnhub_rating.py     # FinnHubRating expert
│   ├── test_trading_agents.py     # TradingAgents (LLM multi-agent)
│   └── test_trading_agents_ui.py  # TradingAgentsUI
└── test_accounts/
    ├── __init__.py
    ├── test_account_interface.py   # Base AccountInterface methods
    ├── test_alpaca_account.py      # AlpacaAccount with mocked SDK
    └── test_ibkr_account.py        # IBKRAccount with mocked API
```

## Test Infrastructure

### conftest.py — Shared Fixtures

**Database isolation (in-memory SQLite):**

```python
import pytest
from sqlmodel import SQLModel, Session, create_engine
from unittest.mock import patch

@pytest.fixture(scope="session")
def test_engine():
    """Create an in-memory SQLite engine for the entire test session."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine

@pytest.fixture(autouse=True)
def patch_db_engine(test_engine):
    """Monkeypatch the production engine so all db.py functions use the test DB."""
    with patch("ba2_trade_platform.core.db.engine", test_engine):
        yield

@pytest.fixture
def db_session(test_engine):
    """Provide a fresh session per test, rolled back after use."""
    with Session(test_engine) as session:
        yield session
        session.rollback()
```

**Mock account fixture:**

```python
@pytest.fixture
def mock_account(db_session):
    """A concrete AccountInterface subclass with canned broker responses."""
    # Creates AccountDefinition in test DB
    # Returns MockAccount instance with:
    #   get_balance() -> 100_000.0
    #   get_positions() -> []
    #   submit_order() -> returns the order with FILLED status
    #   get_current_price("AAPL") -> 150.0
    ...
```

**Mock expert fixture:**

```python
@pytest.fixture
def mock_expert(db_session, mock_account):
    """A concrete MarketExpertInterface subclass with canned analysis results."""
    # Creates ExpertInstance in test DB linked to mock_account
    # Returns MockExpert instance with:
    #   run_analysis() -> ExpertRecommendation(BUY, confidence=75, ...)
    #   get_enabled_instruments() -> ["AAPL", "MSFT"]
    ...
```

### factories.py — Model Factories

Helper functions to create test data with sensible defaults:

```python
def create_account_definition(name="Test Account", provider="mock", **kwargs) -> AccountDefinition
def create_expert_instance(account_id, expert="MockExpert", **kwargs) -> ExpertInstance
def create_recommendation(instance_id, symbol="AAPL", action=OrderRecommendation.BUY, ...) -> ExpertRecommendation
def create_trading_order(transaction_id, symbol="AAPL", ...) -> TradingOrder
def create_transaction(symbol="AAPL", quantity=10, side=OrderDirection.BUY, ...) -> Transaction
def create_ruleset(name="Test Ruleset", ...) -> Ruleset
def create_event_action(name="Test Rule", triggers={...}, actions={...}, ...) -> EventAction
```

All factory functions accept `**kwargs` overrides and return the created instance with its DB-assigned ID.

## Test Module Details

### test_db.py — Database Operations

| Test | Description |
|------|-------------|
| `test_add_instance_returns_id` | Insert model, verify positive integer ID returned |
| `test_add_instance_retrievable` | Insert then get_instance, verify fields match |
| `test_update_instance_persists` | Modify field, update, re-fetch, verify change |
| `test_update_instance_cross_session` | Update instance from different session (merge behavior) |
| `test_delete_instance_removes` | Delete, verify get_instance raises |
| `test_delete_nonexistent_returns_false` | Delete with bad ID returns False |
| `test_get_instance_not_found_raises` | get_instance with bad ID raises Exception |
| `test_get_all_instances_returns_list` | Insert 3, get_all returns 3 |
| `test_get_all_instances_empty` | No records returns empty list |
| `test_get_setting_found` | Insert AppSetting, retrieve by key |
| `test_get_setting_not_found` | Missing key returns None |
| `test_retry_on_lock_retries` | Patch to raise "database is locked" first 2 calls, succeed on 3rd |
| `test_retry_on_lock_gives_up` | Patch to always raise, verify max retries then raises |
| `test_reorder_ruleset_rules` | Create links with order, reorder, verify new order |
| `test_move_rule_up` | Move middle rule up, verify swap |
| `test_move_rule_down` | Move middle rule down, verify swap |
| `test_move_rule_up_at_top` | Already at top, returns False |
| `test_move_rule_down_at_bottom` | Already at bottom, returns False |
| `test_log_activity_queued` | Call log_activity, verify item in queue |

### test_models.py — Model Methods

| Test | Description |
|------|-------------|
| `test_transaction_get_current_open_qty_no_orders` | No orders → 0 |
| `test_transaction_get_current_open_qty_filled` | With filled orders → correct sum |
| `test_transaction_as_string` | Verify string format |
| `test_market_analysis_state_default` | State defaults to {} when None passed |
| `test_trading_order_relationships` | Order linked to transaction correctly |

### test_types.py — Enum Helpers

| Test | Description |
|------|-------------|
| `test_terminal_statuses_complete` | All 7 terminal statuses present |
| `test_executed_statuses` | FILLED and PARTIALLY_FILLED |
| `test_unfilled_statuses` | All pending/waiting statuses |
| `test_unsent_statuses` | PENDING and WAITING_TRIGGER only |
| `test_is_numeric_event_true` | N_ prefixed events return True |
| `test_is_numeric_event_false` | F_ prefixed events return False |
| `test_is_adjustment_action` | ADJUST_TAKE_PROFIT/STOP_LOSS |
| `test_display_label_buy` | "buy" → "bullish (buy)" |
| `test_display_label_sell` | "sell" → "bearish (sell)" |
| `test_display_label_other` | "adjust_take_profit" → "Adjust Take Profit" |

### test_trade_conditions.py — Condition Evaluation

Tests for each condition subclass, parametrized over True/False scenarios:

| Condition Group | Tests |
|----------------|-------|
| **Bullish/Bearish** | BUY recommendation → bullish=True, bearish=False; SELL → opposite |
| **HasPosition** | Mock account returns position → True; no position → False |
| **HasBuyPosition/HasSellPosition** | Direction-specific position checks |
| **RatingTransitions** | Previous vs current recommendation changes (positive→negative, etc.) |
| **Risk Level** | LOW/MEDIUM/HIGH from recommendation matches condition |
| **Time Horizon** | SHORT/MEDIUM/LONG from recommendation matches condition |
| **Numeric: Confidence** | Parametrize with >, >=, <, <=, == operators and boundary values |
| **Numeric: ProfitLossPercent** | With mock existing order + current price, verify calculation |
| **Numeric: DaysOpened** | Mock order open_date, verify day count comparison |
| **Numeric: ExpectedProfitTarget** | From recommendation expected_profit_percent |
| **Numeric: InstrumentAccountShare** | Position value / account balance percentage |
| **Edge cases** | None existing_order, None confidence, zero price |

### test_trade_actions.py — Action Execution

| Action | Tests |
|--------|-------|
| **BuyAction** | Creates Transaction(side=BUY) + TradingOrder, submits to broker, returns success result |
| **BuyAction failure** | Mock submit_order returns None → result.success=False |
| **SellAction** | Creates Transaction(side=SELL), correct direction |
| **CloseAction** | Closes existing transaction, creates closing order |
| **AdjustTakeProfitAction** | Updates TP on existing order, verifies new value |
| **AdjustStopLossAction** | Updates SL on existing order |
| **IncreaseInstrumentShare** | Calculates additional quantity, creates additional buy order |
| **DecreaseInstrumentShare** | Calculates reduction quantity, creates partial close order |
| **submit_to_broker=False** | Action creates order as PENDING, does not call account |

### test_trade_evaluator.py — Ruleset Evaluation

| Test | Description |
|------|-------------|
| `test_evaluate_matching_rule` | Ruleset with 1 rule, conditions match → returns action |
| `test_evaluate_no_match` | Conditions don't match → empty list |
| `test_evaluate_multiple_rules_first_match` | 2 rules, first matches, continue_processing=False → 1 action |
| `test_evaluate_continue_processing` | 2 rules, first matches, continue_processing=True → evaluates second |
| `test_evaluate_all_conditions_debug` | Debug mode evaluates all conditions even after failure |
| `test_execute_actions` | evaluate() then execute() → TradeActionResults created |

### test_trade_manager.py — Trade Manager

| Test | Description |
|------|-------------|
| `test_trigger_and_place_order_match` | Status matches trigger → order submitted, status=OPEN |
| `test_trigger_and_place_order_mismatch` | Status doesn't match → no submission |
| `test_trigger_and_place_order_failure` | submit_order returns None → error logged |
| `test_refresh_accounts` | Mock accounts refreshed |
| `test_concurrent_processing_lock` | Same expert+usecase → second call blocked |

### test_utils.py — Utility Functions

| Test | Description |
|------|-------------|
| `test_close_transaction_with_logging` | Closes transaction, calculates P&L, creates activity log |
| `test_get_expert_instance_from_id` | Returns correct expert class instance |
| `test_get_expert_instance_from_id_cached` | Second call returns same object |
| `test_get_expert_instance_from_id_unknown` | Unknown expert type raises ValueError |
| `test_get_account_instance_from_id` | Returns correct account class instance |

### test_settings.py — Settings Interface

| Test | Description |
|------|-------------|
| `test_load_settings_from_db` | Settings stored in DB loaded correctly |
| `test_save_settings_to_db` | Settings saved and retrievable |
| `test_setting_type_coercion_bool` | "true"/"false" → True/False |
| `test_setting_type_coercion_float` | "3.14" → 3.14 |
| `test_setting_type_coercion_json` | JSON string → dict |
| `test_get_setting_with_interface_default` | Missing setting falls back to definition default |
| `test_merged_settings_definitions` | Builtin + implementation settings merged |

### test_experts/ — Expert-Specific Tests

**test_fmp_rating.py:**

| Test | Description |
|------|-------------|
| `test_settings_definitions` | Returns api_key, threshold, etc. |
| `test_run_analysis_buy_signal` | Mock FMP API returns positive rating → BUY recommendation |
| `test_run_analysis_sell_signal` | Mock negative rating → SELL |
| `test_run_analysis_hold_signal` | Mock neutral rating → HOLD |
| `test_run_analysis_api_error` | Mock API failure → ERROR recommendation or raises |
| `test_confidence_scale` | Confidence in 1-100 range |

**test_finnhub_rating.py:**

| Test | Description |
|------|-------------|
| `test_settings_definitions` | Returns expected keys |
| `test_run_analysis_with_ratings` | Mock Finnhub ratings → valid recommendation |
| `test_run_analysis_no_ratings` | No data available → HOLD |
| `test_api_error_handling` | API timeout → graceful error |

**test_fmp_senate_copy.py / test_fmp_senate_weight.py:**

| Test | Description |
|------|-------------|
| `test_settings_definitions` | Senate-specific settings |
| `test_run_analysis_with_trades` | Mock senate trades → recommendation |
| `test_copy_signal_generation` | Verify copy trade logic |
| `test_weight_calculation` | Verify weighting algorithm |

**test_trading_agents.py:**

| Test | Description |
|------|-------------|
| `test_settings_definitions` | LLM model, temperature, etc. |
| `test_run_analysis_full_flow` | Mock entire LangGraph flow (analyst→researcher→trader), verify final recommendation |
| `test_analyst_team_output` | Mock analyst agents, verify structured output |
| `test_recommendation_extraction` | Parse agent output → ExpertRecommendation |
| `test_llm_api_failure` | OpenAI API error → analysis FAILED |
| `test_streaming_response` | Mock streaming LLM response handling |

### test_accounts/ — Account-Specific Tests

**test_account_interface.py:**

| Test | Description |
|------|-------------|
| `test_validate_trading_order_valid` | Valid order passes validation |
| `test_validate_trading_order_invalid_qty` | Zero/negative quantity rejected |
| `test_validate_closing_order_skips_size_check` | is_closing_order=True bypasses position size limit |
| `test_price_cache_hit` | Cached price returned without API call |
| `test_price_cache_miss` | Expired cache triggers API call |
| `test_open_buy_position` | Creates Transaction(BUY) + TradingOrder |
| `test_open_sell_position` | Creates Transaction(SELL) + TradingOrder |
| `test_close_transaction` | Creates closing order with is_closing_order=True |

**test_alpaca_account.py:**

| Test | Description |
|------|-------------|
| `test_get_balance` | Mock alpaca REST → returns float balance |
| `test_get_positions` | Mock alpaca REST → returns position list |
| `test_submit_order_market` | Verify correct Alpaca OrderRequest params |
| `test_submit_order_oco` | OCO order creates correct leg structure |
| `test_get_orders` | Mock order list retrieval |
| `test_api_auth_failure` | Invalid credentials → clear error |

**test_ibkr_account.py:**

| Test | Description |
|------|-------------|
| `test_get_balance` | Mock IBKR API → balance |
| `test_get_positions` | Mock positions |
| `test_submit_order` | Verify IBKR order parameters |

### Remaining Modules

**test_worker_queue.py:** Task creation, queue ordering, worker thread simulation
**test_smart_priority_queue.py:** Round-robin fairness, priority within expert, mixed priorities (migrate from existing test_files/)
**test_rules_export_import.py:** Export to JSON, import back, verify data integrity
**test_trade_risk.py:** Risk calculations, position sizing
**test_transaction_helper.py:** Helper method tests

## Mocking Strategy

### Fixture-based approach

All mocks are pytest fixtures in `conftest.py`:

1. **MockAccount** — Concrete `AccountInterface` subclass:
   - All abstract methods implemented with configurable return values
   - `submit_order()` defaults to returning the order with FILLED status
   - `get_current_price()` returns from a configurable price dict
   - `get_balance()` returns configurable balance (default 100,000)

2. **MockExpert** — Concrete `MarketExpertInterface` subclass:
   - `run_analysis()` returns configurable `ExpertRecommendation`
   - `get_enabled_instruments()` returns configurable symbol list

3. **External API mocks** — Use `unittest.mock.patch`:
   - `@patch("alpaca.trading.TradingClient")` for Alpaca tests
   - `@patch("requests.get")` for FMP/Finnhub HTTP calls
   - `@patch("openai.OpenAI")` for LLM calls
   - `@patch("langchain_openai.ChatOpenAI")` for LangChain agent calls

### No real API calls rule

Every test module that touches external APIs must use `@pytest.mark.no_network` or equivalent fixture that patches network calls. CI should fail if any test makes a real HTTP request.

## Running Tests

```bash
# Run all tests
pytest tests/

# Run specific module
pytest tests/test_db.py

# Run with coverage
pytest tests/ --cov=ba2_trade_platform --cov-report=term-missing

# Run only fast tests (exclude slow integration)
pytest tests/ -m "not slow"

# Verbose output
pytest tests/ -v
```

## Implementation Order

1. **conftest.py + factories.py** — Test infrastructure first
2. **test_types.py** — Pure functions, no DB needed, validates foundation
3. **test_db.py** — Validates test DB setup works correctly
4. **test_models.py** — Model methods
5. **test_settings.py** — Settings interface (needed by experts/accounts)
6. **test_trade_conditions.py** — Condition evaluation logic
7. **test_trade_actions.py** — Action execution logic
8. **test_trade_evaluator.py** — Ruleset evaluation
9. **test_trade_manager.py** — Order placement flow
10. **test_utils.py** — Utility functions
11. **test_accounts/** — Account interface + implementations
12. **test_experts/** — Expert implementations
13. **test_worker_queue.py + test_smart_priority_queue.py** — Queue logic
14. **test_rules_export_import.py** — Export/import
15. **test_trade_risk.py + test_transaction_helper.py** — Remaining modules
