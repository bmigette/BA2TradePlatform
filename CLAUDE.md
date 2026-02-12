# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BA2 Trade Platform is a Python-based algorithmic trading platform featuring AI-driven market analysis, multi-agent trading strategies, and a plugin architecture for accounts and market experts. Built with SQLModel ORM, NiceGUI web interface, and the TradingAgents multi-agent LLM framework.

## Common Commands

### Running the Application
```bash
# Windows
.venv\Scripts\python.exe main.py

# Linux/macOS
.venv/bin/python main.py

# With custom options
python main.py --port 9090 --db-file ./dev.db
```

### Installing Dependencies
```bash
# With uv (recommended - faster)
uv pip install -r requirements.txt

# With pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Database Migrations (Alembic)
```bash
python migrate.py create "Description of changes"  # Create migration
python migrate.py upgrade                          # Apply migrations
python migrate.py downgrade -1                     # Rollback one revision
python migrate.py current                          # Check current revision
```

### Running Tests
```bash
# Unit tests (pytest)
.venv\Scripts\python.exe -m pytest              # Run all tests
.venv\Scripts\python.exe -m pytest -x            # Stop on first failure
.venv\Scripts\python.exe -m pytest -k "test_name" # Run specific test

# Legacy test files
.venv\Scripts\python.exe test_files/test_name.py
```

### PyTorch / Transformers
PyTorch is a transitive dependency (via `transformers` used by `langchain_core`). On Windows, use the **CPU-only** build to avoid CUDA DLL issues:
```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
```
Do NOT upgrade torch to the latest (e.g. 2.10+) blindly - it causes `OSError: [WinError 1114]` DLL load failures on Windows. Pin to a known working version (e.g. `torch==2.6.0+cpu`).

## Architecture

### Plugin System
- **AccountInterface**: Base class for broker integrations (e.g., `AlpacaAccount`, `IBKRAccount`)
- **MarketExpertInterface**: Base class for AI trading experts (e.g., `TradingAgents`, `FMPRating`)
- **ExtendableSettingsInterface**: Shared settings management via database key-value storage

### Core Directory Structure
```
ba2_trade_platform/
├── core/                    # Interfaces, models, utilities
│   ├── interfaces/          # Abstract base classes (AccountInterface, MarketExpertInterface)
│   ├── models.py            # SQLModel database models
│   ├── types.py             # Enums (OrderStatus, OrderDirection, RiskLevel, etc.)
│   ├── db.py                # Database helpers (get_instance, add_instance, update_instance)
│   ├── utils.py             # Shared utilities (get_expert_instance_from_id, etc.)
│   ├── TradeManager.py      # Order processing and recommendation handling
│   ├── JobManager.py        # Background job scheduling
│   └── WorkerQueue.py       # Task queue for parallel processing
├── modules/
│   ├── accounts/            # Broker implementations (AlpacaAccount, IBKRAccount)
│   ├── experts/             # Expert implementations (TradingAgents, FMPRating, etc.)
│   └── dataproviders/       # Market data providers (news, indicators, OHLCV, etc.)
├── ui/                      # NiceGUI web interface
│   ├── main.py              # Route definitions
│   └── pages/               # Page components
├── thirdparties/TradingAgents/  # Multi-agent LLM framework
├── config.py                # Global configuration
└── logger.py                # Centralized logging
```

### Database
- SQLite at `~/Documents/ba2_trade_platform/db.sqlite`
- Key models: `AccountDefinition`, `ExpertInstance`, `ExpertRecommendation`, `MarketAnalysis`, `TradingOrder`, `Transaction`, `Ruleset`, `EventAction`

### TradingAgents Framework
Located in `ba2_trade_platform/thirdparties/TradingAgents/`. Multi-agent system with:
- Analyst team (Fundamentals, Sentiment, News, Technical)
- Researcher team (Bull/Bear debates)
- Trader agent and Risk Management

## Critical Patterns

### Avoid Code Duplication
Check `core/utils.py` for existing helpers before writing new code. Key functions:
- `close_transaction_with_logging()` - Transaction closures with P&L
- `log_close_order_activity()` - Activity logging for orders
- `get_expert_instance_from_id()` - Cached expert instance retrieval
- `get_account_instance_from_id()` - Cached account instance retrieval

### Database Operations
```python
from ba2_trade_platform.core.db import get_instance, add_instance, update_instance
from ba2_trade_platform.core.models import ExpertInstance

expert = get_instance(ExpertInstance, expert_id)
new_id = add_instance(new_expert)
```

### Configuration Access - No Defaults
Always use explicit dict access, never `.get()` with defaults:
```python
# CORRECT
model = config["quick_think_llm"]

# WRONG - hides missing config
model = config.get("quick_think_llm", "gpt-3.5-turbo")
```

### Logging
```python
from ba2_trade_platform.logger import logger

logger.info("General info")
logger.debug("Debug info")

# ONLY use exc_info=True inside except blocks
try:
    risky_operation()
except Exception as e:
    logger.error(f"Failed: {e}", exc_info=True)
```

### Live Data - No Fallbacks
Never use default values for prices, balances, or quantities:
```python
# WRONG
price = recommendation.current_price or 1.0

# CORRECT
if price is None:
    raise ValueError("Price not available")
```

### Confidence Values
Always stored as 1-100 scale (not 0-1):
```python
confidence = 78.1  # Means 78.1%
print(f"{confidence:.1f}%")  # "78.1%"
```

### Data Provider format_type
Providers with `format_type` parameter must support three formats:
- `"markdown"` (default): Returns markdown string for LLM consumption
- `"dict"`: Returns JSON-serializable Python dict (NO markdown)
- `"both"`: Returns dict with `"text"` and `"data"` keys

### AI-Friendly API Design
Prefer explicit function names over string parameters for AI agents:
```python
# CORRECT
def open_buy_position(self, symbol, quantity, ...): ...
def open_sell_position(self, symbol, quantity, ...): ...

# WRONG - AI can confuse "LONG"/"BUY", "SHORT"/"SELL"
def open_position(self, symbol, direction: str, ...): ...
```

## Settings System

Both accounts and experts use the ExtendableSettingsInterface pattern:
```python
class MyExpert(MarketExpertInterface):
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
            "api_key": {"type": "str", "required": True, "description": "API Key"},
            "threshold": {"type": "float", "required": True, "default": 0.5}
        }

    def __init__(self, id: int):
        super().__init__(id)
        # Access via self.settings["api_key"]
```

## Web Interface

- Runs on port 8080 by default (NiceGUI)
- Main routes defined in `ba2_trade_platform/ui/main.py`
- Access at http://localhost:8080
- Settings configuration at http://localhost:8080/settings

## Environment Variables

Configure in `.env` file:
- `OPENAI_API_KEY` - Required for AI experts
- `FINNHUB_API_KEY` - Market data and news
- `ALPHA_VANTAGE_API_KEY` - Additional market data
- `PRICE_CACHE_TIME` - Cache duration in seconds (default: 60)

Most configuration is done via the web UI Settings page rather than environment variables.
