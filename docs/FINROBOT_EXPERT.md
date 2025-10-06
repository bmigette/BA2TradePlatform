# FinRobot Expert Integration

## Overview

The FinRobotExpert is a new market analysis expert integrated into the BA2 Trade Platform. It leverages FinRobot's AI Agent Platform for financial analysis using Large Language Models to provide comprehensive market insights.

## What is FinRobot?

FinRobot is an open-source AI Agent Platform designed specifically for financial applications. It uses:
- **Large Language Models (LLMs)** for intelligent analysis
- **Multi-source data integration** from market feeds, news, and financial statements
- **AutoGen framework** for agent-based AI workflows
- **Specialized financial tools** for data retrieval and analysis

## Files Created/Modified

### New Files

1. **`ba2_trade_platform/modules/experts/FinRobotExpert.py`**
   - Main expert class implementing `MarketExpertInterface`
   - Wraps FinRobot's Market_Analyst agent
   - Handles prediction and analysis workflows
   - Thread-safe with instance-specific locks

2. **`ba2_trade_platform/thirdparties/FinRobot/finrobot/data_source/ba2_yfinance_adapter.py`**
   - Adapter to use BA2's YFinanceDataProvider instead of direct yfinance calls
   - Provides caching and data management integration
   - Compatible interface matching FinRobot's expected YFinanceUtils

3. **`docs/FINROBOT_EXPERT.md`** (this file)
   - Documentation for the FinRobot expert integration

### Modified Files

1. **`ba2_trade_platform/modules/experts/__init__.py`**
   - Added FinRobotExpert to experts list
   - Expert is now available in the system

2. **`ba2_trade_platform/thirdparties/FinRobot/finrobot/data_source/__init__.py`**
   - Modified to use BA2's YFinance adapter instead of direct yfinance
   - Ensures all FinRobot data source calls use our caching system

3. **`ba2_trade_platform/thirdparties/FinRobot/finrobot/utils.py`**
   - Added logger import
   - Replaced print statements with logger.info()

4. **`ba2_trade_platform/thirdparties/FinRobot/finrobot/data_source/finnhub_utils.py`**
   - Added logger import
   - Replaced print statements with logger.warning() and logger.debug()

## Expert Settings

The FinRobotExpert provides the following configurable settings:

### LLM Configuration

- **llm_model** (default: "gpt-4-0125-preview")
  - Available models: gpt-4-0125-preview, gpt-4-turbo, gpt-4, gpt-3.5-turbo, gpt-4o, gpt-4o-mini
  - Controls which OpenAI model is used for analysis
  
- **temperature** (default: 0.0)
  - Range: 0.0 to 1.0
  - Controls response randomness (0 = deterministic, 1 = creative)
  - Recommended: 0-0.3 for financial analysis
  
- **timeout** (default: 120 seconds)
  - Maximum time to wait for LLM responses
  - Increase if experiencing timeout errors

### Analysis Configuration

- **human_input_mode** (default: "NEVER")
  - Options: NEVER, ALWAYS, TERMINATE
  - Controls automation level
  - Use "NEVER" for fully automated trading
  
- **max_auto_reply** (default: 10)
  - Limits consecutive agent responses
  - Prevents infinite loops
  - Recommended: 5-15

### Data Lookback Periods

- **news_lookback_days** (default: 7)
  - Days of news data to analyze
  - Recommended: 3-14 days
  
- **market_data_days** (default: 90)
  - Historical price data window
  - Recommended: 60-180 days

### Code Execution

- **use_docker** (default: False)
  - Run generated code in isolated Docker containers
  - Requires Docker installed
  
- **work_dir** (default: "finrobot_workspace")
  - Directory for generated code and analysis files

### Analysis Components

- **include_news** (default: True)
  - Include recent company news in analysis
  
- **include_financials** (default: True)
  - Include financial statements analysis
  
- **include_technical** (default: True)
  - Include technical analysis and indicators

## How It Works

### 1. Initialization

```python
expert = FinRobotExpert(instance_id)
```

- Loads expert instance from database
- Sets up API keys from application config (OPENAI_API_KEY, FINNHUB_API_KEY)
- Configures LLM settings
- Creates instance-specific thread lock for safety

### 2. Quick Prediction

```python
signal = expert.get_prediction_for_instrument("AAPL")
```

- Creates a Market_Analyst agent
- Builds analysis prompt based on settings
- Gets LLM-powered prediction
- Parses response for BUY/SELL/HOLD signal
- Returns OrderRecommendation

### 3. Full Market Analysis

```python
analysis_id = expert.run_market_analysis("AAPL", AnalysisUseCase.ENTER_MARKET)
```

- Creates MarketAnalysis record with RUNNING status
- Creates Market_Analyst agent
- Runs comprehensive analysis including:
  - Recent news analysis (if enabled)
  - Financial statements review (if enabled)
  - Technical indicators (if enabled)
- Parses full response for recommendation
- Gets current price from YFinanceDataProvider
- Creates ExpertRecommendation record with:
  - Signal (BUY/SELL/HOLD)
  - Confidence (0-100%)
  - Expected profit percentage
  - Risk level (LOW/MEDIUM/HIGH)
  - Time horizon (SHORT_TERM/MEDIUM_TERM/LONG_TERM)
- Creates AnalysisOutput with full analysis text
- Updates MarketAnalysis status to COMPLETED
- Returns analysis_id

## Thread Safety

The FinRobotExpert is designed for multi-threaded execution:

- **Class-level lock dictionary**: Maintains locks for each expert instance
- **Instance-specific locks**: Each expert instance has its own threading.Lock
- **Lock acquisition**: Critical sections (agent creation, API calls) are protected
- **No shared state**: Each analysis creates its own agent instance

## Data Provider Integration

The expert uses BA2's YFinanceDataProvider through a custom adapter:

### Benefits

1. **Caching**: Avoids redundant API calls by using cached data
2. **Rate limiting**: Respects Yahoo Finance API limits
3. **Error handling**: Consistent error handling across platform
4. **Logging**: All data access is logged through our logger

### How It Works

```python
# FinRobot code calls
YFinanceUtils.get_stock_data("AAPL", "2024-01-01", "2024-12-31")

# Internally routes to
BA2YFinanceAdapter.get_stock_data()
  -> YFinanceDataProvider.get_historical_data()
     -> Cached or fetches from Yahoo Finance
```

## Logging

All FinRobot operations are logged through BA2's centralized logging system:

- **Debug logs**: Agent creation, configuration, data retrieval
- **Info logs**: Analysis start/completion, recommendations
- **Warning logs**: Missing API keys, no data found
- **Error logs**: Exceptions with full stack traces

Example logs:
```
[INFO] Starting FinRobot market analysis for AAPL (subtype: ENTER_MARKET)
[DEBUG] Created Market_Analyst agent for expert instance 3
[INFO] Retrieved stock data for AAPL from 2024-09-01 to 2024-12-31: 90 rows
[INFO] Parsed recommendation for AAPL: BUY (confidence: 75.5%, expected: +3.2%)
[INFO] Completed FinRobot analysis 42 for AAPL: BUY
```

## API Keys Required

The FinRobotExpert requires the following API keys in application configuration:

1. **OPENAI_API_KEY** (required)
   - OpenAI API key for LLM access
   - Set in application config or .env file
   
2. **FINNHUB_API_KEY** (optional but recommended)
   - Finnhub API key for company news and financials
   - Free tier available at https://finnhub.io
   - Set via config or environment variable

## Example Usage

### Creating a New Expert Instance

1. Navigate to Settings → Expert Instances
2. Click "Add Expert Instance"
3. Select "FinRobotExpert" from dropdown
4. Configure settings:
   - Set shortname: e.g., "FinRobot - GPT-4 Analyst"
   - Choose LLM model: gpt-4-0125-preview or gpt-4o
   - Set temperature: 0.0 for deterministic
   - Enable analysis components: news, financials, technical
5. Enable instruments (symbols) to analyze
6. Save

### Running Analysis Manually

```python
from ba2_trade_platform.modules.experts import get_expert_class

# Get expert class
FinRobotExpert = get_expert_class("FinRobotExpert")

# Create instance
expert = FinRobotExpert(instance_id=3)

# Run analysis
analysis_id = expert.run_market_analysis("NVDA", AnalysisUseCase.ENTER_MARKET)
print(f"Analysis ID: {analysis_id}")
```

### Viewing Results

1. Navigate to Market Analysis → Job Monitoring
2. Find your analysis by ID
3. Click "View Details" to see:
   - Full analysis text
   - Recommendation (BUY/SELL/HOLD)
   - Confidence percentage
   - Expected price movement
   - Risk assessment

## Comparison with TradingAgents

| Feature | FinRobot | TradingAgents |
|---------|----------|---------------|
| **AI Framework** | AutoGen | Custom Graph |
| **Analysis Style** | LLM-based conversational | Multi-agent debate |
| **Data Sources** | News, financials, technical | News, technical, fundamental, macro |
| **Debates** | N/A (single agent) | 2-4 rounds configurable |
| **Cost** | OpenAI API per analysis | OpenAI API per analysis |
| **Speed** | Fast (single pass) | Slower (multiple debate rounds) |
| **Customization** | High (prompt-based) | High (agent configuration) |
| **Best For** | Quick insights, financial statements | Deep analysis, multi-perspective |

## Troubleshooting

### "OPENAI_API_KEY not configured"
- Ensure OPENAI_API_KEY is set in application config or .env file
- Restart application after adding key

### "Timeout" errors
- Increase timeout setting (Settings → Expert Instances → Edit)
- Try a faster model (gpt-3.5-turbo or gpt-4o-mini)

### No recommendations generated
- Check logs for errors
- Verify FINNHUB_API_KEY is set for news access
- Ensure symbol is valid and has recent data

### "Import autogen could not be resolved"
- This is expected - autogen is imported at runtime from FinRobot
- Ensure FinRobot requirements are installed in virtual environment

## Future Enhancements

Potential improvements for the FinRobotExpert:

1. **Multi-agent workflows**: Implement group chat with multiple specialized agents
2. **RAG integration**: Add document retrieval for SEC filings analysis
3. **Custom toolkits**: Create BA2-specific tools for portfolio analysis
4. **Caching**: Cache LLM responses for repeated analyses
5. **Streaming**: Stream analysis results as they're generated
6. **Fine-tuning**: Fine-tune LLMs on historical trading data

## Dependencies

The FinRobot expert relies on:

- **autogen**: AI agent framework
- **openai**: OpenAI API client
- **finnhub-python**: Finnhub API client
- **yfinance**: Yahoo Finance data (via BA2 adapter)
- **pandas**: Data manipulation
- **python-dotenv**: Environment variable management

These are included in FinRobot's requirements.txt.

## References

- [FinRobot GitHub](https://github.com/AI4Finance-Foundation/FinRobot)
- [FinRobot Whitepaper](https://arxiv.org/abs/2405.14767)
- [AutoGen Documentation](https://microsoft.github.io/autogen/)
- [OpenAI API Documentation](https://platform.openai.com/docs/)
- [Finnhub API Documentation](https://finnhub.io/docs/api)
