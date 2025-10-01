# BA2 Trade Platform

A sophisticated Python-based algorithmic trading platform featuring AI-driven market analysis, multi-agent trading strategies, and a comprehensive plugin architecture for accounts and market experts.

## ⚠️ IMPORTANT DISCLAIMER

**THIS SOFTWARE IS IN BETA AND PROVIDED "AS-IS" WITHOUT WARRANTY OF ANY KIND.**

- 🚨 **Trading involves substantial risk of loss** and is not suitable for all investors
- 🧪 **This software is experimental** and should be thoroughly tested in paper trading mode before considering live trading
- 💰 **You can lose money** - possibly all of your investment capital
- 🤖 **AI-driven decisions are not infallible** - algorithms can make mistakes, markets are unpredictable
- 📉 **Past performance does not guarantee future results** - backtesting and historical analysis may not reflect real trading conditions
- ⚙️ **Software bugs may exist** - thoroughly review all code and test extensively before use
- 🔒 **Use at your own risk and discretion** - you are solely responsible for any trading decisions and their outcomes
- 💼 **Not financial advice** - this platform is a tool for educational and research purposes

**RECOMMENDED PRACTICES:**
- ✅ Start with paper trading to familiarize yourself with the platform
- ✅ Set strict risk limits and position sizing rules
- ✅ Monitor all automated trades closely
- ✅ Never invest more than you can afford to lose
- ✅ Understand the underlying strategies and code before enabling automation
- ✅ Keep detailed logs and review trading decisions regularly
- ✅ Test thoroughly in various market conditions before live deployment

By using this software, you acknowledge that you understand and accept these risks.

## 🚀 Features

### Core Platform
- **Plugin Architecture**: Extensible system for trading accounts and market experts
- **SQLModel ORM**: Modern database layer with SQLite backend
- **NiceGUI Web Interface**: Clean, responsive web UI for configuration and monitoring
- **Extensible Settings**: Flexible configuration system for all plugins
- **Centralized Logging**: Comprehensive logging with file rotation and colored output

### AI Trading Agents
- **Multiple Expert Support**: Extensible plugin architecture supporting multiple expert types (currently TradingAgents)
- **Parallel Market Analysis**: Simultaneous analysis across multiple symbols for efficient processing
- **Multi-Agent Analysis**: Market, news, fundamentals, social media, and macro-economic analysts
- **TradingAgents Integration**: Advanced multi-agent LLM framework for financial trading
- **FRED API Integration**: Real-time macroeconomic data analysis
- **Debate-Based Decision Making**: Bull vs bear researcher debates with research manager oversight
- **Risk Management**: Multi-layered risk analysis and management

### Trading Modes & Risk Management
- **Semi-Automatic Trading**: Human approval required for trade execution
- **Full Automatic Trading**: Autonomous trading based on AI recommendations
- **Virtual Equity Management**: Split account balance across multiple experts to limit individual risk
- **Expert-Level Risk Controls**: Configurable risk limits per expert instance
- **Portfolio Diversification**: Automatic allocation management across different strategies

### Market Data & APIs
- **Multiple Data Sources**: Alpaca, Finnhub, SimFin, Yahoo Finance, FRED
- **Real-Time & Historical Data**: Comprehensive market data coverage
- **Economic Indicators**: Inflation, employment, treasury yields, economic calendar
- **Social Sentiment**: Reddit and social media sentiment analysis

### Trading Features
- **Multi-Expert Support**: Run multiple AI experts simultaneously with individual risk management
- **Parallel Symbol Analysis**: Analyze multiple instruments concurrently for faster decision-making
- **Automated Trade Execution**: Semi-automatic (manual approval) or fully automatic trading modes
- **Virtual Account Splitting**: Allocate portions of your account to different experts to limit exposure
- **Risk-Based Position Sizing**: Dynamic position sizing based on expert confidence and risk assessment
- **Expert Performance Tracking**: Monitor and compare performance across different expert strategies

### Account Providers
- **Alpaca Integration**: Paper and live trading support
- **Extensible Architecture**: Easy addition of new brokers via AccountInterface

## 📋 Requirements

- Python 3.11+
- SQLite (included)
- OpenAI API Key (or compatible LLM provider)
- Optional: Alpaca API Key, Finnhub API Key, FRED API Key

## 🛠️ Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/bmigette/BA2TradePlatform.git
   cd BA2TradePlatform
   ```

2. **Create virtual environment**:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   # or
   source .venv/bin/activate  # Linux/Mac
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the application**:
   ```bash
   python main.py
   ```

5. **Access the web interface**:
   Open http://localhost:8080 in your browser

## 🏗️ Architecture

### Core Interfaces

#### AccountInterface
Abstract base class for trading account implementations:
```python
class AccountInterface(ExtendableSettingsInterface):
    def get_account_info(self) -> dict
    def submit_order(self, order_data: dict) -> dict
    def get_positions(self) -> List[dict]
    def get_orders(self) -> List[dict]
```

#### MarketExpertInterface  
Abstract base class for AI trading experts:
```python
class MarketExpertInterface(ExtendableSettingsInterface):
    def get_prediction_for_instrument(self, symbol: str) -> dict
    def get_analysis_for_instruments(self, symbols: List[str]) -> dict
```

#### ExtendableSettingsInterface
Base class providing flexible configuration:
```python
@classmethod
def get_settings_definitions(cls) -> Dict[str, Any]:
    return {
        "setting_name": {
            "type": "str",
            "required": True, 
            "description": "Setting description"
        }
    }
```

### Database Models

**Core Models** (in `ba2_trade_platform/core/models.py`):
- `AppSetting`: Application-wide configuration (API keys, settings)
- `AccountDefinition`: Trading account provider configurations
- `AccountSetting`: Account-specific settings (key-value storage)
- `ExpertInstance`: AI expert configurations with virtual equity allocation and rulesets
- `ExpertSetting`: Expert-specific settings (key-value storage)
- `ExpertRecommendation`: Trading recommendations with risk level, time horizon, and confidence
- `MarketAnalysis`: Analysis sessions with status tracking and expert linking
- `AnalysisOutput`: Detailed analysis outputs from individual agents
- `TradingOrder`: Order lifecycle tracking (PENDING → OPEN → FILLED/CLOSED)
- `Transaction`: Transaction history for orders (fills, partial fills)
- `Position`: Current positions with P&L tracking
- `Instrument`: Instrument metadata (symbols, exchanges, asset classes)
- `Ruleset`: Rule-based trading logic containers
- `EventAction`: Conditional actions within rulesets
- `RulesetEventActionLink`: Many-to-many relationship for rulesets and actions
- `TradeActionResult`: Results from executed trade actions (BUY, SELL, CLOSE, etc.)

### Directory Structure

```
ba2_trade_platform/
├── core/                           # Core interfaces and models
│   ├── AccountInterface.py         # Account provider interface
│   ├── MarketExpertInterface.py    # Expert interface
│   ├── ExtendableSettingsInterface.py # Settings management
│   ├── models.py                   # SQLModel database models
│   ├── types.py                    # Enums (OrderStatus, OrderDirection, RiskLevel, etc.)
│   ├── db.py                       # Database utilities (CRUD operations)
│   ├── utils.py                    # Helper functions
│   ├── actions.py                  # Trade action helpers
│   ├── TradeManager.py             # Order processing and recommendation handling
│   ├── TradeActionEvaluator.py     # Ruleset evaluation engine
│   ├── TradeActions.py             # Trade action implementations (BUY, SELL, CLOSE)
│   ├── TradeConditions.py          # Condition evaluation for rulesets
│   ├── TradeRiskManagement.py      # Risk management and position sizing
│   ├── JobManager.py               # Background job scheduling
│   ├── WorkerQueue.py              # Task queue for parallel processing
│   ├── MarketAnalysisPDFExport.py  # Export analysis to PDF reports
│   ├── rules_documentation.py      # Ruleset documentation generator
│   └── rules_export_import.py      # Import/export rulesets
├── modules/
│   ├── accounts/                   # Account implementations
│   │   ├── __init__.py            # Account registry
│   │   └── AlpacaAccount.py        # Alpaca integration
│   ├── experts/                    # Expert implementations
│   │   ├── __init__.py            # Expert registry
│   │   └── TradingAgents.py        # Multi-agent LLM expert
│   └── marketinfo/                 # Market information providers
├── thirdparties/
│   └── TradingAgents/              # TradingAgents multi-agent framework
├── ui/                             # NiceGUI web interface
│   ├── main.py                     # Route definitions and app initialization
│   ├── layout.py                   # Page layout components
│   ├── menus.py                    # Navigation menus
│   ├── svg.py                      # SVG icon utilities
│   ├── pages/                      # Page components
│   │   ├── overview.py            # Dashboard and account overview
│   │   ├── marketanalysis.py       # Market analysis management
│   │   └── settings.py            # Configuration interface
│   ├── components/                 # Reusable UI components
│   │   └── InstrumentSelector.py   # Instrument selection widget
│   └── static/                     # Static assets (favicons, etc.)
├── logs/                           # Application logs
├── config.py                       # Global configuration
└── logger.py                       # Centralized logging
```

## 🤖 TradingAgents Framework

The platform integrates the TradingAgents multi-agent framework for sophisticated market analysis:

### Agent Types
- **Market Analyst**: Technical analysis and price patterns
- **News Analyst**: News sentiment and impact analysis  
- **Fundamentals Analyst**: Company financials and metrics
- **Social Media Analyst**: Social sentiment analysis
- **Macro Analyst**: Economic indicators and macro trends
- **Bull/Bear Researchers**: Debate-based analysis
- **Research Manager**: Synthesis and final recommendations

### Analysis Workflow
1. **Data Collection**: Multi-source data gathering
2. **Agent Analysis**: Parallel analysis by specialized agents
3. **Debate Phase**: Bull vs bear researcher arguments
4. **Synthesis**: Research manager consolidation
5. **Risk Assessment**: Multi-perspective risk analysis
6. **Final Recommendation**: Trading decision with confidence levels

### Usage Examples

**Standalone Analysis** (terminal output):
```python
from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.graph.trading_graph import TradingAgentsGraph
from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.default_config import DEFAULT_CONFIG

# Create analyzer without database storage
ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG)

# Run analysis
final_state, decision = ta.propagate("AAPL", "2025-01-15")
print(f"Decision: {decision}")
```

**Database-Stored Analysis**:
```python
# Create analyzer with expert instance ID for database storage
ta = TradingAgentsGraph(
    debug=True, 
    config=DEFAULT_CONFIG,
    expert_instance_id=1  # Links to ExpertInstance in database
)

# Analysis results automatically stored in database
final_state, decision = ta.propagate("NVDA", "2025-01-15")
```

**Parallel Multi-Symbol Analysis**:
```python
# Analyze multiple symbols simultaneously
symbols = ["AAPL", "GOOGL", "MSFT", "NVDA"]
expert_instances = [1, 2, 3]  # Multiple experts

# Platform automatically manages parallel analysis across symbols and experts
# Results stored with risk levels, time horizons, and expert attribution
```

**Virtual Equity Management**:
```python
# Expert configuration with virtual equity allocation
expert_config = {
    "virtual_equity_percent": 25.0,  # 25% of total account
    "max_position_percent": 5.0,    # Max 5% per position
    "trading_mode": "semi_auto",     # Requires approval
    "risk_tolerance": "MEDIUM"       # Risk level preference
}
```

## 🎛️ Configuration

### Web Interface
Access the settings page at http://localhost:8080/settings to configure:
- API Keys (OpenAI, Finnhub, FRED)
- Account Providers (Alpaca credentials)
- Expert Settings (TradingAgents parameters)

### Environment Variables
```env
# LLM Configuration
OPENAI_API_KEY=your_key_here

# Market Data
FINNHUB_API_KEY=your_key_here
FRED_API_KEY=your_key_here

# Trading Accounts  
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets  # Paper trading
```

### Logging Configuration
Modify `ba2_trade_platform/config.py`:
```python
STDOUT_LOGGING = True   # Console output
FILE_LOGGING = True     # File logging with rotation
```

## 🔌 Extending the Platform

### Adding New Account Provider

1. **Create provider class**:
```python
from ba2_trade_platform.core.AccountInterface import AccountInterface

class MyBrokerAccount(AccountInterface):
    @classmethod
    def get_settings_definitions(cls):
        return {
            "api_key": {"type": "str", "required": True},
            "paper_trading": {"type": "bool", "required": True}
        }
    
    def get_account_info(self):
        # Implementation here
        pass
```

2. **Register in UI**: The provider will automatically appear in the web interface

### Adding New Market Expert

1. **Create expert class**:
```python
from ba2_trade_platform.core.MarketExpertInterface import MarketExpertInterface

class MyExpert(MarketExpertInterface):
    @classmethod  
    def get_settings_definitions(cls):
        return {
            "model_type": {"type": "str", "required": True},
            "confidence_threshold": {"type": "float", "required": True}
        }
    
    def get_prediction_for_instrument(self, symbol: str):
        # Implementation here
        pass
```

## 📊 Database Schema

The platform uses SQLModel for ORM with automatic SQLite database creation:

**Key Tables**:
- `appsetting`: Application-wide configuration and API keys
- `accountdefinition`: Trading account provider configurations
- `accountsetting`: Account-specific settings (key-value)
- `expertinstance`: AI expert configurations with rulesets and virtual equity
- `expertsetting`: Expert-specific settings (key-value)
- `expertrecommendation`: Trading recommendations with risk/confidence metrics
- `marketanalysis`: Analysis job tracking with status and timing
- `analysisoutput`: Detailed outputs from individual analysis agents
- `tradingorder`: Order lifecycle and execution tracking
- `transaction`: Transaction history for order fills
- `position`: Current positions with unrealized P&L
- `instrument`: Instrument metadata and specifications
- `ruleset`: Rule-based trading logic containers
- `eventaction`: Conditional actions (triggers and actions)
- `ruleseteventactionlink`: Many-to-many relationship for rulesets
- `tradeactionresult`: Results from executed trade actions

**Database Features**:
- Automatic schema creation and migrations via Alembic
- SQLite backend with full ACID compliance
- Foreign key constraints for data integrity
- Indexed fields for query performance

Database auto-initializes at: `~/Documents/ba2_trade_platform/db.sqlite`

## 🧪 Testing

**Run TradingAgents test**:
```bash
python test_trade_agents.py
```

**Basic functionality test**:
```bash
python test.py
```

## 📝 Logging

**File Locations**:
- Main logs: `ba2_trade_platform/logs/app.log`
- Debug logs: `ba2_trade_platform/logs/app.debug.log` 
- TradingAgents logs: `./tradeagents-exp{id}.log`

**Log Features**:
- Automatic rotation (10MB max, 5 backups)
- Colored console output with icons
- Expert-specific log files
- Configurable log levels

## 🔧 Development

**Project Structure**:
- Core interfaces in `ba2_trade_platform/core/`
- Implementations in `ba2_trade_platform/modules/`
- Web UI in `ba2_trade_platform/ui/`
- Third-party integrations in `ba2_trade_platform/thirdparties/`

**Adding Dependencies**:
```bash
pip install new_package
pip freeze > requirements.txt
```

## 🚀 Production Deployment

1. **Set production environment variables**
2. **Configure proper API keys in database**
3. **Enable file logging**: Set `FILE_LOGGING = True`
4. **Run with production WSGI server** (if needed)
5. **Set up proper database backup strategy**

## 🐛 Troubleshooting

**Common Issues**:

1. **Import Errors**: Ensure all dependencies installed with `pip install -r requirements.txt`

2. **Database Issues**: Database auto-creates on first run. Check permissions in `~/Documents/`

3. **API Key Issues**: Configure keys via web interface at `/settings` or environment variables

4. **Unicode Console Errors**: Logger automatically falls back to ASCII on Windows

5. **Memory Issues**: ChromaDB collections are created in memory - restart if needed

**Debug Mode**:
```python
ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG)
```

## 📚 Documentation

- **Core Interfaces**: See docstrings in `ba2_trade_platform/core/`
- **API Reference**: Auto-generated from type hints
- **Examples**: Check `test_trade_agents.py` and `test.py`

## 🤝 Contributing

1. Fork the repository
2. Create feature branch: `git checkout -b feature-name`
3. Make changes with proper tests
4. Submit pull request

## 📄 License

[Add your license information here]

---

# Credits

Project that uses *TradingAgents*  https://github.com/TauricResearch/TradingAgents

```
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework}, 
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138}, 
}
```