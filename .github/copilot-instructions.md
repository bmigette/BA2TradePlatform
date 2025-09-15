# BA2 Trade Platform - AI Assistant Instructions

## Project Overview
BA2 Trade Platform is a Python-based algorithmic trading platform that integrates various market data sources, technical analysis tools, and AI/ML capabilities for automated trading.

## Architecture
- `ba2_trade_platform/` - Main package directory
  - `modules/` - Core functional modules
    - `accounts/` - Trading account management
    - `marketexperts/` - Market analysis and trading strategies
  - `ui/` - User interface components
  - `config.py` - Global configuration settings
  - `logger.py` - Centralized logging configuration

## Key Patterns and Conventions
1. **Logging**:
   - Uses Python's built-in logging with both file and stdout output
   - Log files are stored in `logs/` directory with rotation (10MB max size, 7 backups)
   - Debug level for detailed logs (`app.debug.log`) and Info level for main logs (`app.log`)

2. **Configuration**:
   - Core settings in `config.py`
   - Uses environment-based configuration with `HOME` and `HOME_PARENT` path variables
   - Feature flags like `STDOUT_LOGGING` and `FILE_LOGGING` control logging behavior

## Dependencies
Key integrations include:
- Market Data: `yfinance`, `eodhd`, `akshare`, `tushare`, `finnhub-python`
- Trading: `backtrader`, `ibind`
- AI/ML: `langchain` ecosystem (`langchain-openai`, `langchain-anthropic`, `langchain-google-genai`)
- Data Storage: `chromadb`, `redis`, `sqlalchemy`
- UI: `nicegui`, `chainlit`, `rich`

## Development Workflow
1. **Python Environment**:
   - Project uses Python virtual environments
   - Install dependencies: `pip install -r requirements.txt`

2. **Project Setup**:
   - Ensure `logs/` directory exists for logging
   - Configure required API keys for data providers
   - Set up database connections if using persistent storage

## Best Practices
1. **Logging**:
   ```python
   from ba2_trade_platform.logger import logger
   
   logger.debug("Detailed debug info")
   logger.info("General execution info")
   logger.error("Error conditions")
   ```

2. **Error Handling**:
   - Use proper exception handling with logging
   - Implement graceful degradation for market data sources