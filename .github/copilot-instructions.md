# BA2 Trade Platform - AI Assistant Instructions

## Project Overview
BA2 Trade Platform is a Python-based algorithmic trading platform built around a plugin architecture for accounts and market experts. It features a SQLModel-based ORM, NiceGUI web interface, and extensible settings system for AI-driven trading strategies.

## Core Architecture

### Plugin System
- **Account Interfaces**: Implement `AccountInterface` for different brokers (e.g., `AlpacaAccount`)
- **Market Experts**: Implement `MarketExpertInterface` for AI trading strategies (e.g., `TradingAgents`)
- **Extensible Settings**: Both interfaces extend `ExtendableSettingsInterface` for flexible configuration

### Database Layer
- **SQLModel ORM**: All models in `ba2_trade_platform/core/models.py`
- **SQLite Database**: Located at `~/Documents/ba2_trade_platform/db.sqlite`
- **Database Functions**: Use `ba2_trade_platform/core/db.py` helpers (`get_instance`, `add_instance`, etc.)

### Directory Structure
```
ba2_trade_platform/
├── core/                    # Core interfaces and data models
│   ├── AccountInterface.py  # Abstract base for trading accounts
│   ├── MarketExpertInterface.py  # Abstract base for AI experts
│   ├── ExtendableSettingsInterface.py  # Settings management
│   ├── models.py           # SQLModel database models
│   ├── types.py            # Enums (OrderStatus, ExpertActionType, etc.)
│   ├── db.py              # Database utilities
│   └── utils.py           # Shared utility functions (close_transaction_with_logging, etc.)
├── modules/
│   ├── accounts/          # Account implementations (AlpacaAccount)
│   └── experts/           # Expert implementations (TradingAgents)
├── ui/                    # NiceGUI web interface
│   ├── main.py           # Route definitions
│   ├── pages/            # Page components
│   └── components/       # Reusable UI components
├── config.py             # Global configuration and environment variables
└── logger.py             # Centralized logging setup
```

## Key Patterns

### 1. **CRITICAL: Avoid Code Duplication**
**MOST IMPORTANT RULE**: Before writing any code, check if similar functionality already exists. If it does, refactor to use/extend the existing code instead of duplicating.

**Common Anti-Patterns to Avoid**:
- ❌ Copying try-except blocks with log_activity() calls
- ❌ Duplicating transaction closing logic in multiple places
- ❌ Repeating activity logging patterns with slight variations
- ❌ Copy-pasting order submission, cancellation, or validation logic

**Proper Approach**:
- ✅ **ALWAYS check `core/utils.py` first** for existing helper functions
- ✅ **Create helper functions** in `core/utils.py` for repeated patterns
- ✅ **Extract common logic** into centralized functions with clear parameters
- ✅ **Reuse existing functions** even if they need small modifications - extend them!

**Example of Good Practice**:
```python
# ❌ BAD: Duplicated logging code
try:
    from ..db import log_activity
    from ..types import ActivityLogSeverity, ActivityLogType
    log_activity(
        severity=ActivityLogSeverity.SUCCESS,
        activity_type=ActivityLogType.TRANSACTION_CLOSED,
        description=f"Submitted closing order for {symbol}",
        data={...},
        source_account_id=self.id,
        source_expert_id=expert_id
    )
except Exception as e:
    logger.warning(f"Failed to log: {e}")

# ✅ GOOD: Use centralized helper function
from ..utils import log_close_order_activity
log_close_order_activity(
    transaction=transaction,
    account_id=self.id,
    success=True,
    close_order_id=order_id
)
```

**Existing Helper Functions in `core/utils.py`**:
- `close_transaction_with_logging()` - Close transactions with P&L calculation and activity logging
- `log_close_order_activity()` - Log close order submission (success/failure/retry)
- `get_account_instance_from_id()` - Get cached account instances

**When to Create New Helper Functions**:
- When you find yourself writing similar code in 2+ places
- When error handling + logging patterns repeat
- When business logic (like order validation, price calculations) is duplicated
- When database operations follow the same pattern

**Refactoring Checklist**:
1. Search for similar code patterns in the codebase (`grep_search`, `semantic_search`)
2. Identify what varies and what stays the same
3. Extract common logic into a function with parameters for variations
4. Add the function to `core/utils.py` with clear docstring
5. Replace all instances with calls to the new function
6. Test to ensure behavior is preserved

### 2. **Settings Management**
All plugins use the ExtendableSettingsInterface pattern:
```python
class MyAccount(AccountInterface):
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
            "api_key": {"type": "str", "required": True, "description": "API Key"},
            "paper_account": {"type": "bool", "required": True, "description": "Paper trading?"}
        }
    
    def __init__(self, id: int):
        super().__init__(id)
        # Access settings via self.settings["api_key"]
```

### 2. **Type System**
Use enums from `core/types.py` for consistency:
- `OrderStatus`, `OrderDirection`, `OrderType` for trading
- `ExpertActionType`, `ExpertEventType` for AI recommendations
- `InstrumentType` for asset classifications

### 3. **Database Operations**
Always use the core database helpers:
```python
from ba2_trade_platform.core.db import get_instance, add_instance, update_instance
from ba2_trade_platform.core.models import ExpertInstance

# Get instance
expert = get_instance(ExpertInstance, expert_id)

# Create new instance
new_expert = ExpertInstance(account_id=1, expert="TradingAgents")
expert_id = add_instance(new_expert)
```

### 4. **Logging**
Centralized logging with file rotation:
```python
from ba2_trade_platform.logger import logger

logger.debug("Detailed debug info")  # Goes to app.debug.log
logger.info("General execution info")  # Goes to both logs
logger.error("Error conditions", exc_info=True)  # Include exc_info=True ONLY in exception handlers
```

**Critical Logging Rules**:

1. **`exc_info=True` Parameter Usage**:
   - ✅ **ONLY use `exc_info=True` inside `except` blocks** where you have an active exception context
   - ❌ **NEVER use `exc_info=True` outside exception handlers** - it will cause `KeyError('exc_info')`
   
   **Correct Usage (inside exception handler)**:
   ```python
   try:
       result = risky_operation()
   except Exception as e:
       logger.error(f"Operation failed: {e}", exc_info=True)  # ✅ Correct - has exception context
   ```
   
   **Incorrect Usage (outside exception handler)**:
   ```python
   if result is None:
       logger.error("Result is None", exc_info=True)  # ❌ Wrong - no exception context, will cause KeyError
   ```
   
   **Fix for non-exception errors**:
   ```python
   if result is None:
       logger.error("Result is None")  # ✅ Correct - no exc_info parameter
   ```

2. **When to include `exc_info=True`**:
   - Use it in `except` blocks to capture full stack traces for debugging
   - Helps identify the root cause of exceptions
   - Essential for production error tracking

3. **When NOT to include `exc_info=True`**:
   - Simple validation failures (e.g., `if x is None`)
   - Expected error conditions (e.g., "Record not found")
   - Any error logging outside an `except` block

### 5. **Configuration Access - NEVER Use Defaults**
**CRITICAL RULE**: Always access configuration with explicit dict access `config["key"]`, NEVER with `config.get("key", default)`:
- ✅ **ALWAYS DO THIS**: `model = config["quick_think_llm"]` - Will raise `KeyError` if missing
- ✅ **ALWAYS DO THIS**: `lookback_days = config["news_lookback_days"]` - Fails loudly on missing config
- ❌ **NEVER DO THIS**: `model = config.get("quick_think_llm", "gpt-3.5-turbo")` - Hides configuration errors
- ❌ **NEVER DO THIS**: `lookback_days = config.get("news_lookback_days", 7)` - Uses unpredictable defaults

**Rationale**: Silent fallbacks lead to unpredictable behavior. Configuration errors should fail immediately and explicitly. This ensures:
- Missing required configuration is caught early during development
- No silent mode switches (e.g., gpt-3.5-turbo instead of intended model)
- Explicit error messages for troubleshooting (KeyError tells you exactly which config key is missing)

**Exception Handling Pattern**:
```python
try:
    model = config["quick_think_llm"]
    lookback_days = config["news_lookback_days"]
except KeyError as e:
    logger.error(f"Missing required configuration key: {e}", exc_info=True)
    raise  # Let it fail explicitly for operator to fix configuration
```

### 6. **Live Data Handling**
**CRITICAL RULE**: Never use default values or fallbacks for live market data (prices, balances, quantities, etc.):
- ❌ **NEVER DO THIS**: `price = recommendation.current_price or 1.0`
- ❌ **NEVER DO THIS**: `balance = account.get_balance() or 0.0`
- ✅ **ALWAYS DO THIS**: Check for `None` explicitly and raise an error or skip the operation
- ✅ **ALWAYS DO THIS**: Get real-time prices from account interface using `account.get_instrument_current_price(symbol)`

**Rationale**: Using default values for financial data can lead to catastrophic trading errors, incorrect position sizing, and financial loss. Always fail explicitly rather than proceeding with fake data.

### 7. **Confidence Level Handling**
**CRITICAL RULE**: Confidence values are **always stored as 1-100 scale** in the database, never divided or multiplied:
- ✅ **ALWAYS DO THIS**: Store confidence as 1-100 (e.g., 78.1 means 78.1% confidence)
- ✅ **ALWAYS DO THIS**: Display using `f"{confidence:.1f}%"` format (e.g., "78.1%")
- ❌ **NEVER DO THIS**: `confidence * 100` or `confidence / 100` when reading/writing
- ❌ **NEVER DO THIS**: Use `.1%` format string (expects 0-1 scale, we use 1-100)

**Examples:**
```python
# ✅ Correct - Store as 1-100
confidence = 78.1
expert_rec.confidence = round(confidence, 1)

# ✅ Correct - Display as number with % sign
print(f"Confidence: {confidence:.1f}%")  # "Confidence: 78.1%"

# ❌ Wrong - Don't multiply/divide
confidence = recommendation.confidence * 100  # NO!

# ❌ Wrong - Don't use .1% format
print(f"Confidence: {confidence:.1%}")  # Expects 0-1, we have 1-100!
```

**Rationale**: Consistency across all experts and UI components. All confidence values use the same 1-100 scale to avoid conversion errors and confusion.

### 8. **Data Provider format_type Parameter**
**CRITICAL RULE**: All providers with `format_type` parameter MUST support all three formats with consistent behavior:

```python
format_type: Literal["dict", "markdown", "both"] = "markdown"
```

**Format Semantics**:
1. **format_type="markdown"** (DEFAULT): Returns markdown string for LLM consumption
2. **format_type="dict"** (CRITICAL): Returns JSON-serializable Python dict with STRUCTURED DATA ONLY (NO markdown)
3. **format_type="both"**: Returns dict with `"text"` (markdown) and `"data"` (structured dict) keys

**Dict Format MUST Be Clean**:
- ✅ JSON-serializable types only (str, int, float, bool, list, dict, null)
- ✅ ISO 8601 strings for dates (not datetime objects)
- ✅ Direct arrays: `"dates": [...], "values": [...]` for visualization
- ❌ NO markdown content wrapped in dict
- ❌ NO Python objects (datetime, etc.)

**Implementation Pattern**:
```python
def get_indicator(self, ..., format_type: Literal["dict", "markdown", "both"] = "markdown"):
    # Build CLEAN structured response (no markdown)
    structured_response = {
        "symbol": "AAPL",
        "dates": ["2025-09-22T09:30:00", ...],  # Direct arrays!
        "values": [72.5, 71.2, ...],
        "metadata": {"count": 2, "description": "..."}
    }
    
    # Build markdown response (can include data points)
    markdown_response = {
        "data": [{"date": ..., "value": ...}],  # OK for markdown
        "metadata": {...}
    }
    
    if format_type == "dict":
        return structured_response
    elif format_type == "both":
        return {"text": self._format_markdown(markdown_response), "data": structured_response}
    else:  # markdown
        return self._format_markdown(markdown_response)
```

**Rationale**: Separating concerns prevents visualization tools from parsing markdown. Structured dict enables direct data binding without string manipulation. See `docs/DATA_PROVIDER_FORMAT_SPECIFICATION.md` for complete specification.

### 9. **AI-Friendly API Design**
**CRITICAL RULE**: When designing APIs for AI agents (like Smart Risk Manager), prefer explicit function names over string parameters:

- ✅ **ALWAYS DO THIS**: Create separate functions (`open_buy_position()`, `open_sell_position()`)
- ❌ **NEVER DO THIS**: Single function with string parameter (`open_position(direction="BUY")`)

**Rationale**: AI models can confuse semantically equivalent terms (e.g., "LONG" vs "BUY", "SHORT" vs "SELL"). Explicit function names:
1. Are self-documenting
2. Eliminate parameter validation errors
3. Make invalid states unrepresentable
4. Provide better IDE/AI autocomplete

**Example Pattern:**
```python
# ❌ Error-prone: AI might use "LONG" instead of "BUY"
def open_position(self, symbol, direction: str, quantity, ...):
    direction = OrderDirection(direction)  # Can fail!

# ✅ Clear: No ambiguity possible
def open_buy_position(self, symbol, quantity, ...):
    return self._open_internal(symbol, OrderDirection.BUY, quantity, ...)

def open_sell_position(self, symbol, quantity, ...):
    return self._open_internal(symbol, OrderDirection.SELL, quantity, ...)
```

See `docs/SMART_RISK_MANAGER_FUNCTION_SPLIT.md` for complete case study.

### 10. **Activity Logging - Use Helper Functions**
**CRITICAL RULE**: NEVER duplicate activity logging code. Always use or create helper functions in `core/utils.py`.

**Existing Helper Functions**:
- `close_transaction_with_logging()` - For transaction closures with P&L calculation
- `log_close_order_activity()` - For close order submissions (success/failure/retry)

**Pattern for New Activity Logging**:
```python
# ❌ BAD: Duplicated 50+ lines of try-except with log_activity
try:
    from ..db import log_activity
    from ..types import ActivityLogSeverity, ActivityLogType
    log_activity(severity=..., activity_type=..., description=..., data={...})
except Exception as e:
    logger.warning(f"Failed to log: {e}")

# ✅ GOOD: Create/use helper function
from ..utils import log_transaction_created_activity
log_transaction_created_activity(transaction, account_id, success=True)
```

**When You Need Activity Logging**:
1. Check if a helper function exists in `core/utils.py`
2. If it exists, use it!
3. If it doesn't exist but similar patterns do, refactor to create one
4. Add proper docstring explaining parameters and usage

## Dependencies
- **Trading**: `alpaca-py` (primary broker), `yfinance`, `backtrader`
- **AI/ML**: `langchain-*` ecosystem, `stockstats`
- **Data**: `sqlmodel`, `chromadb`, `redis`, `pandas`
- **UI**: `nicegui`, `rich`, `questionary`
- **External APIs**: `finnhub-python`, `eodhd`, `tushare`, `akshare`

## Common Tasks

### Adding New Account Provider
1. Create class in `modules/accounts/` extending `AccountInterface`
2. Implement all abstract methods (`get_account_info`, `submit_order`, etc.)
3. Define `get_settings_definitions()` for required configuration

### Adding New Market Expert
1. Create class in `modules/experts/` extending `MarketExpertInterface`
2. Implement prediction methods (`get_prediction_for_instrument`, etc.)
3. Handle instrument enablement via settings system

### Database Schema Changes
1. Modify models in `core/models.py`
2. Database recreates automatically on next run (SQLite auto-migration)
3. Use proper SQLModel field definitions with relationships

## Development Workflow

### Setup
1. Create and activate virtual environment: `python -m venv .venv` then `.venv\Scripts\Activate.ps1` (Windows) or `source .venv/bin/activate` (Unix)
2. Install dependencies: `.venv\Scripts\python.exe -m pip install -r requirements.txt` (Windows) or `.venv/bin/python -m pip install -r requirements.txt` (Unix)
3. Database auto-initializes on first run via `main.py`
4. Set environment variables in `.env` file (API keys, etc.)

**Important**: Always use the virtual environment's Python executable (`.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on Unix) for all Python commands to ensure proper dependency isolation.

### Running
- **Main Application**: Use virtual environment Python: `.venv\Scripts\python.exe main.py` (Windows) or `.venv/bin/python main.py` (Unix) (starts NiceGUI web interface)
- **Configuration**: Environment variables loaded from `.env` via `config.load_config_from_env()`
- **Virtual Environment**: Always use the project's virtual environment Python executable located at `.venv\Scripts\python.exe` (Windows) or `.venv/bin/python` (Unix)

### Testing
- Manual testing through web UI at http://localhost:8080
- **Test Files**: All test scripts should be created in the `test_files/` directory
- Basic testing utilities: `test.py`
- Run tests with: `.venv\Scripts\python.exe test_files/test_name.py` (Windows) or `.venv/bin/python test_files/test_name.py` (Unix)

## Documentation Policy

**CRITICAL RULE**: Do NOT automatically create documentation files for completed tasks unless explicitly requested by the user.

- ❌ **NEVER DO THIS**: Automatically create `.md` files in `docs/` to summarize work
- ❌ **NEVER DO THIS**: Generate summary documents after completing fixes or features
- ✅ **ALWAYS DO THIS**: Only create documentation when user explicitly asks for it
- ✅ **ALWAYS DO THIS**: Provide verbal summaries in chat responses instead

**Rationale**: Automatic documentation generation clutters the repository and the user prefers to control when documentation is needed. Keep the focus on implementing solutions, not documenting them unless requested.

## Code Review Checklist

Before completing any task, verify:
- [ ] No code duplication - checked for existing similar functions
- [ ] Helper functions used from `core/utils.py` where applicable
- [ ] Activity logging uses centralized helper functions
- [ ] No `exc_info=True` outside exception handlers
- [ ] Configuration access uses `config["key"]` not `.get()`
- [ ] Live data has proper None checks, no default fallbacks
- [ ] Confidence values use 1-100 scale consistently
- [ ] API design uses explicit function names for AI agents
- [ ] Database operations use core helpers (`get_instance`, etc.)
- [ ] Proper error handling with explicit failures
