# BA2 Trade Platform - Expert Documentation

This document provides comprehensive information about all available trading experts in the BA2 Trade Platform. Each expert implements different trading strategies and analysis methodologies to provide trading recommendations based on various data sources and algorithms.

## Overview

The BA2 Trade Platform uses a plugin-based expert system where each expert can:
- Analyze financial instruments using different methodologies
- Generate trading recommendations (BUY/SELL/HOLD)
- Provide confidence scores and expected profit estimates
- Configure instrument selection methods (static, dynamic, or expert-driven)
- Run on customizable schedules

## Available Experts

### 1. TradingAgents
**Multi-agent AI trading system with debate-based analysis and risk assessment**

- **Type**: AI-powered multi-agent system
- **Methodology**: Uses multiple AI agents that debate and analyze market conditions
- **Data Sources**: Market data, financial statements, news sentiment
- **Instrument Selection**: Static/Dynamic (cannot recommend its own instruments)
- **Key Features**:
  - Debate-based analysis with configurable rounds
  - Risk assessment and position sizing
  - Support for both new positions and existing position management
  - Customizable timeframes and analysis depth

**Key Settings** (25 total):
- `debates_new_positions`: Number of debate rounds for new position analysis
- `debates_existing_positions`: Number of debate rounds for existing position analysis  
- `timeframe`: Analysis timeframe for market data
- `use_advanced_analysis`: Enable advanced technical and fundamental analysis
- `risk_tolerance`: Risk tolerance level (conservative, moderate, aggressive)

### 2. FinnHubRating
**Finnhub analyst recommendation trends with weighted confidence scoring**

- **Type**: Analyst consensus aggregator
- **Methodology**: Aggregates analyst recommendations from Finnhub API
- **Data Sources**: Finnhub analyst ratings and price targets
- **Instrument Selection**: Static/Dynamic (cannot recommend its own instruments)
- **Key Features**:
  - Weighted scoring based on recommendation strength
  - Trend analysis of rating changes
  - Confidence scoring based on analyst consensus

**Key Settings** (1 total):
- `strong_factor`: Weight multiplier for strong buy/sell ratings (default: 2.0)

### 3. FMPRating
**FMP analyst price consensus with profit potential calculation**

- **Type**: Price target analyzer
- **Methodology**: Analyzes analyst price targets and consensus ratings
- **Data Sources**: Financial Modeling Prep (FMP) analyst data
- **Instrument Selection**: Static/Dynamic (cannot recommend its own instruments)
- **Key Features**:
  - Price target consensus analysis
  - Profit potential calculation based on current vs target prices
  - Minimum analyst threshold for reliability

**Key Settings** (2 total):
- `profit_ratio`: Profit ratio multiplier for expected profit calculation
- `min_analysts`: Minimum number of analysts required for valid recommendation

### 4. FMPSenateTraderWeight
**Government official trading activity analysis using weighted algorithm based on portfolio allocation**

- **Type**: Government trading tracker (sophisticated algorithm)
- **Methodology**: Weighted algorithm considering portfolio allocation percentages
- **Data Sources**: FMP Senate/House trading disclosure data
- **Instrument Selection**: Static/Dynamic (cannot recommend its own instruments)
- **Key Features**:
  - Portfolio allocation analysis (symbol focus percentage)
  - Historical trader performance evaluation
  - Investment size and timing considerations
  - Age-based filtering for trade relevance
  - Complex confidence calculation based on trader behavior patterns

**Key Settings** (4 total):
- `max_disclose_date_days`: Maximum days since trade disclosure (default: 30)
- `max_trade_exec_days`: Maximum days since trade execution (default: 60)
- `max_trade_price_delta_pct`: Maximum price change since trade (default: 10%)
- `growth_confidence_multiplier`: Multiplier for confidence calculation (default: 5.0)

### 5. FMPSenateTraderCopy
**Copy trades from specific senators/representatives with 100% confidence**

- **Type**: Government trading tracker (simple copy trading)
- **Methodology**: Direct copy trading from specified government officials
- **Data Sources**: FMP Senate/House trading disclosure data
- **Instrument Selection**: Expert-driven (can recommend its own instruments)
- **Key Features**:
  - **Can recommend instruments**: Yes (can select its own trading targets)
  - **Should expand instrument jobs**: False (prevents job duplication)
  - Simple copy trading with fixed confidence and profit targets
  - Follows specific senators/representatives by name
  - Issues only one recommendation per instrument (most recent trade wins)
  - Age-based filtering for trade relevance

**Key Settings** (4 total):
- `copy_trade_names`: Senators/representatives to copy trade (comma-separated, **required**)
- `max_disclose_date_days`: Maximum days since trade disclosure (default: 30)
- `max_trade_exec_days`: Maximum days since trade execution (default: 60)
- `should_expand_instrument_jobs`: Expand instrument jobs (default: False)

## Expert Properties Comparison

| Expert | Can Recommend Instruments | Typical Use Case |
|--------|---------------------------|------------------|
| TradingAgents | No | Complex AI-driven analysis with debate system |
| FinnHubRating | No | Analyst consensus tracking |
| FMPRating | No | Price target analysis |
| FMPSenateTraderWeight | No | Sophisticated government trading analysis |
| FMPSenateTraderCopy | **Yes** | Simple government trade copying |

## Instrument Selection Methods

### Static Selection
- **Manual Configuration**: User manually selects which instruments to analyze
- **Used by**: All experts except FMPSenateTraderCopy in expert mode
- **Best for**: Focused analysis on specific securities

### Dynamic Selection  
- **AI-Driven Prompts**: User provides natural language descriptions of desired instruments
- **Used by**: All experts when configured
- **Best for**: Flexible, criteria-based instrument selection

### Expert-Driven Selection
- **Expert Decides**: The expert algorithm determines which instruments to analyze
- **Used by**: FMPSenateTraderCopy (can recommend its own instruments)
- **Best for**: Autonomous trading systems that discover opportunities

## Job Scheduling and Management

### Standard Experts
- Create scheduled jobs for each enabled instrument
- Follow traditional scheduling patterns
- Suitable for portfolio-based strategies

### Self-Managing Experts (FMPSenateTraderCopy)
- Can recommend their own instruments
- Use `should_expand_instrument_jobs: False` to prevent job duplication
- Run analysis and discover trading opportunities autonomously
- Ideal for discovery-based trading strategies

## Configuration Best Practices

### For Portfolio Management
1. Use **TradingAgents** for comprehensive AI analysis
2. Combine with **FMPRating** or **FinnHubRating** for consensus validation
3. Configure static instrument selection for your portfolio

### For Government Trading Following
1. Use **FMPSenateTraderWeight** for sophisticated analysis of government trading patterns
2. Use **FMPSenateTraderCopy** for simple copy trading of specific officials
3. FMPSenateTraderCopy works best with `should_expand_instrument_jobs: False`

### For Market Discovery
1. **FMPSenateTraderCopy** with expert-driven instrument selection
2. Set up minimal scheduling to let the expert discover opportunities
3. Monitor expert recommendations for new instrument additions

## API Requirements

| Expert | Required API Keys | Data Sources |
|--------|------------------|--------------|
| TradingAgents | Various (OpenAI, etc.) | Multiple AI services |
| FinnHubRating | FINNHUB_API_KEY | Finnhub.io |
| FMPRating | FMP_API_KEY | Financial Modeling Prep |
| FMPSenateTraderWeight | FMP_API_KEY | Financial Modeling Prep |
| FMPSenateTraderCopy | FMP_API_KEY | Financial Modeling Prep |

## Risk and Compliance Notes

### Government Trading Data
- **Data Lag**: Government officials must disclose trades within 30-45 days
- **Price Movement**: Opportunities may be reduced by the time data is available
- **Legal Compliance**: Ensure compliance with local regulations regarding government trading data usage

### AI-Based Analysis
- **Model Limitations**: AI recommendations should be validated with traditional analysis
- **Data Quality**: Ensure high-quality data feeds for optimal AI performance
- **Risk Management**: Always use appropriate position sizing and risk controls

## Contributing New Experts

To add a new expert to the platform:

1. **Create Expert Class**: Extend `MarketExpertInterface`
2. **Implement Required Methods**: 
   - `description()`: Human-readable description
   - `get_settings_definitions()`: Configuration options
   - `run_analysis()`: Main analysis logic
   - `render_market_analysis()`: UI rendering
3. **Define Properties**: Set `can_recommend_instruments` if applicable
4. **Register Expert**: Add to `ba2_trade_platform/modules/experts/__init__.py`
5. **Test Integration**: Verify settings, scheduling, and analysis functionality

For detailed implementation guidelines, see the existing expert implementations in `ba2_trade_platform/modules/experts/`.