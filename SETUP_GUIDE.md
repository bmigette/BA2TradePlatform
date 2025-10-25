# BA2 Trade Platform - Setup & Running Guide

## Overview
BA2 Trade Platform is a Python-based algorithmic trading system with a plugin architecture for accounts and market experts. It features a NiceGUI web interface, SQLModel-based ORM, and extensible settings system for AI-driven trading strategies.

## Quick Start

### Prerequisites
- Python 3.12+
- Linux/Unix environment
- Virtual environment support

### Installation

1. **Clone the repository**
```bash
git clone https://github.com/bmigette/BA2TradePlatform.git
cd BA2TradePlatform
```

2. **Create and activate virtual environment**
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Set up environment variables**
Create a `.env` file in the project root:
```env
FINNHUB_API_KEY=your_key_here
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-5
ALPHA_VANTAGE_API_KEY=your_key_here
```

## Running the Application

### Option 1: Direct Execution (Development)
```bash
venv/bin/python main.py
```
Access the web interface at: **http://localhost:8080**

### Option 2: As a System Service (Production - Recommended)

The app is configured to run as a systemd service for continuous operation:

#### Check Service Status
```bash
sudo systemctl status ba2-trade-platform
```

#### Control the Service
```bash
# Start the service
sudo systemctl start ba2-trade-platform

# Stop the service
sudo systemctl stop ba2-trade-platform

# Restart the service
sudo systemctl restart ba2-trade-platform

# View live logs
sudo journalctl -u ba2-trade-platform -f

# View recent logs
sudo journalctl -u ba2-trade-platform -n 100
```

#### Service Features
- ✅ Auto-starts on server reboot
- ✅ Auto-restarts if it crashes (every 10 seconds)
- ✅ Runs in background without blocking terminal
- ✅ Resource limits (2GB max memory)
- ✅ Centralized logging via journalctl

## Project Structure

```
ba2_trade_platform/
├── core/                    # Core interfaces and data models
│   ├── AccountInterface.py  # Abstract base for trading accounts
│   ├── MarketExpertInterface.py  # Abstract base for AI experts
│   ├── models.py           # SQLModel database models
│   ├── types.py            # Enums (OrderStatus, ExpertActionType, etc.)
│   └── db.py              # Database utilities
├── modules/
│   ├── accounts/          # Account implementations (AlpacaAccount)
│   └── experts/           # Expert implementations (TradingAgents)
├── ui/                    # NiceGUI web interface
│   ├── main.py           # Route definitions
│   ├── pages/            # Page components (overview, settings, etc.)
│   └── components/       # Reusable UI components
├── config.py             # Global configuration
└── logger.py             # Centralized logging

db.sqlite                  # SQLite database (auto-created)
logs/                      # Application logs
```

## Database

### Location
- Database file: `~/Documents/ba2_trade_platform/db.sqlite`
- Cache folder: `~/Documents/ba2_trade_platform/cache`

### Auto-Migration
The database schema is automatically created on first run via SQLModel. If you modify models in `core/models.py`, the database will be updated on next startup.

### Manual Database Column Addition
If you need to add missing columns (like in schema updates):
```bash
venv/bin/python -c "
import sqlite3
db_path = '~/Documents/ba2_trade_platform/db.sqlite'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute('ALTER TABLE table_name ADD COLUMN column_name TYPE')
conn.commit()
conn.close()
"
```

## Web Interface

Access the application at **http://localhost:8080**

### Main Pages
- **Overview**: Dashboard with account info, transactions, and performance metrics
- **Market Analysis**: AI-driven market analysis and recommendations
- **Settings**: Configuration for accounts, experts, rules, and instruments

## Logging

### Log Files
- `logs/app.log` - Main application log
- `logs/app.debug.log` - Debug-level logs

### View Logs
```bash
# Via systemd (recommended)
sudo journalctl -u ba2-trade-platform -f

# Via log files
tail -f logs/app.log
```

### Log Levels
- `DEBUG` - Detailed development information
- `INFO` - General execution flow
- `WARNING` - Warning conditions
- `ERROR` - Error conditions (non-critical)
- `CRITICAL` - Critical errors (app may fail)

## Configuration

### Application Settings
Global configuration is in `ba2_trade_platform/config.py`:
- `DB_FILE` - Database location
- `CACHE_FOLDER` - Cache directory
- `PRICE_CACHE_TIME` - Price cache duration (seconds)
- `LOG_FOLDER` - Logs directory

### Environment Variables
Set in `.env` file:
- `FINNHUB_API_KEY` - Finnhub API key
- `OPENAI_API_KEY` - OpenAI API key
- `OPENAI_BACKEND_URL` - OpenAI API endpoint (default: https://api.openai.com/v1)
- `OPENAI_MODEL` - Default LLM model (default: gpt-5)
- `ALPHA_VANTAGE_API_KEY` - Alpha Vantage API key
- `PRICE_CACHE_TIME` - Price cache duration

### Database App Settings
Additional settings can be stored in the database via the `AppSetting` model:
```python
from ba2_trade_platform.config import get_app_setting, set_app_setting

# Get a setting
api_key = get_app_setting('alpaca_market_api_key')

# Set a setting
set_app_setting('alpaca_market_api_key', 'PKxxxx')
```

## Key Components

### Job Manager
Handles scheduled analysis and account refresh jobs. Runs in background and automatically syncs transaction states.

### Worker Queue
Processes AI analysis tasks concurrently with 4 worker threads. Handles market analysis, expert predictions, and trading recommendations.

### Smart Risk Manager
Monitors open positions and enforces risk management rules. Manages stop losses, take profits, and position sizing.

### Trade Manager
Manages all trading operations, order synchronization, and transaction tracking.

## Troubleshooting

### App Won't Start
1. Check service status: `sudo systemctl status ba2-trade-platform`
2. View logs: `sudo journalctl -u ba2-trade-platform -n 50`
3. Verify database exists: `ls -la ~/Documents/ba2_trade_platform/`

### Database Errors
1. Check database file: `sqlite3 ~/Documents/ba2_trade_platform/db.sqlite ".tables"`
2. Verify permissions: `ls -la ~/Documents/ba2_trade_platform/`
3. Backup and reset: Move old DB, app will auto-create new one

### Service Won't Start on Boot
1. Verify service is enabled: `sudo systemctl is-enabled ba2-trade-platform`
2. Enable if needed: `sudo systemctl enable ba2-trade-platform`
3. Check service file: `sudo cat /etc/systemd/system/ba2-trade-platform.service`

### High Memory Usage
Check current memory: `ps aux | grep "python.*main"`
Service is limited to 2GB max (configurable in service file)

## Development

### Running in Development Mode
```bash
source venv/bin/activate
python main.py
```
This will show output directly in terminal and auto-reload on code changes.

### Testing
Test files should be in `test_files/` directory:
```bash
venv/bin/python test_files/test_name.py
```

### Git Workflow
Current branch: `report-ui`
```bash
git status
git add .
git commit -m "Your message"
git push origin report-ui
```

## Architecture

### Plugin System
- **AccountInterface**: Implement for different brokers (e.g., Alpaca)
- **MarketExpertInterface**: Implement AI trading strategies
- **ExtendableSettingsInterface**: Flexible configuration system

### Database Layer
- **SQLModel ORM**: Type-safe SQL operations
- **SQLite**: Local database with WAL mode
- Helper functions in `db.py`: `get_instance()`, `add_instance()`, `update_instance()`

### Type System
Consistent enums in `core/types.py`:
- `OrderStatus`, `OrderDirection`, `OrderType` - Trading types
- `ExpertActionType`, `ExpertEventType` - AI recommendation types
- `InstrumentType` - Asset classifications

## Support

For issues or questions:
1. Check logs: `sudo journalctl -u ba2-trade-platform -f`
2. Review documentation in `docs/` folder
3. Check GitHub repository: https://github.com/bmigette/BA2TradePlatform

## License

See LICENSE file in repository root.

---

**Last Updated**: October 25, 2025
**Current Branch**: report-ui
**Status**: ✅ Production Ready
