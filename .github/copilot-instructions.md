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
│   └── db.py              # Database utilities
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

### 1. **Settings Management**
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
logger.error("Error conditions", exc_info=True)  # ALWAYS include exc_info=True for error logs
```

**Critical Logging Rule**: Always include `exc_info=True` parameter in all `logger.error()` calls to capture full stack traces for debugging. This is mandatory for all error logging throughout the codebase.

### 5. **Live Data Handling**
**CRITICAL RULE**: Never use default values or fallbacks for live market data (prices, balances, quantities, etc.):
- ❌ **NEVER DO THIS**: `price = recommendation.current_price or 1.0`
- ❌ **NEVER DO THIS**: `balance = account.get_balance() or 0.0`
- ✅ **ALWAYS DO THIS**: Check for `None` explicitly and raise an error or skip the operation
- ✅ **ALWAYS DO THIS**: Get real-time prices from account interface using `account.get_instrument_current_price(symbol)`

**Rationale**: Using default values for financial data can lead to catastrophic trading errors, incorrect position sizing, and financial loss. Always fail explicitly rather than proceeding with fake data.

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
- Test file: `test.py` (basic testing utilities)

