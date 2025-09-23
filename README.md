# BA2 Trade Platform

A sophisticated Python-based algorithmic trading platform featuring AI-driven market analysis, multi-agent trading strategies, and a comprehensive plugin architecture for accounts and market experts.

## 🚀 Features

### Core Platform
- **Plugin Architecture**: Extensible system for trading accounts and market experts
- **SQLModel ORM**: Modern database layer with SQLite backend
- **NiceGUI Web Interface**: Clean, responsive web UI for configuration and monitoring
- **Extensible Settings**: Flexible configuration system for all plugins
- **Centralized Logging**: Comprehensive logging with file rotation and colored output

### AI Trading Agents
- **Multi-Agent Analysis**: Market, news, fundamentals, social media, and macro-economic analysts
- **TradingAgents Integration**: Advanced multi-agent LLM framework for financial trading
- **FRED API Integration**: Real-time macroeconomic data analysis
- **Debate-Based Decision Making**: Bull vs bear researcher debates with research manager oversight
- **Risk Management**: Multi-layered risk analysis and management

### Market Data & APIs
- **Multiple Data Sources**: Alpaca, Finnhub, SimFin, Yahoo Finance, FRED
- **Real-Time & Historical Data**: Comprehensive market data coverage
- **Economic Indicators**: Inflation, employment, treasury yields, economic calendar
- **Social Sentiment**: Reddit and social media sentiment analysis

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

4. **Configure environment** (optional):
   Create a `.env` file with your API keys:
   ```env
   OPENAI_API_KEY=your_openai_api_key
   FINNHUB_API_KEY=your_finnhub_api_key
   FRED_API_KEY=your_fred_api_key
   ```

5. **Run the application**:
   ```bash
   python main.py
   ```

6. **Access the web interface**:
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
- `AccountInstance`: Trading account configurations
- `ExpertInstance`: AI expert configurations  
- `ExpertRecommendation`: Trading recommendations
- `MarketAnalysis`: Analysis sessions
- `AnalysisOutput`: Detailed analysis outputs
- `AppSetting`: Application configuration

### Directory Structure

```
ba2_trade_platform/
├── core/                           # Core interfaces and models
│   ├── AccountInterface.py         # Account provider interface
│   ├── MarketExpertInterface.py    # Expert interface
│   ├── ExtendableSettingsInterface.py # Settings management
│   ├── models.py                   # Database models
│   ├── types.py                    # Enums and types
│   └── db.py                       # Database utilities
├── modules/
│   ├── accounts/                   # Account implementations
│   │   └── AlpacaAccount.py        # Alpaca integration
│   └── experts/                    # Expert implementations
│       └── TradingAgents.py        # Multi-agent expert
├── thirdparties/
│   └── TradingAgents/              # TradingAgents framework
├── ui/                             # NiceGUI web interface
│   ├── main.py                     # Route definitions
│   ├── pages/                      # Page components
│   └── components/                 # Reusable UI components
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
- `accountinstance`: Trading account configurations
- `expertinstance`: AI expert configurations  
- `expertrecommendation`: Trading recommendations
- `marketanalysis`: Analysis sessions
- `analysisoutput`: Detailed analysis outputs
- `appsetting`: Application settings

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