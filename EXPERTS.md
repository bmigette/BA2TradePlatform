# BA2 Trade Platform - Expert Documentation

This document provides comprehensive information about all available trading experts in the BA2 Trade Platform. Each expert implements different trading strategies and analysis methodologies to provide trading recommendations based on various data sources and algorithms.

## Overview

The BA2 Trade Platform uses a plugin-based expert system where each expert can:
- Analyze financial instruments using different methodologies
- Either **generate trading recommendations** (BUY/SELL/HOLD, scored by the SmartRiskManager) **or self-execute** their own orders (e.g. PennyMomentumTrader, FactorRanker)
- Provide confidence scores and expected profit estimates
- Configure instrument selection methods (static, dynamic, expert-driven, or screener)
- Run on customizable weekly or monthly schedules

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

### 6. PennyMomentumTrader
**Live intraday penny-stock momentum trader with catalyst triggers and staged exits**

- **Type**: Live, self-executing momentum trader (`LiveExpertInterface`)
- **Methodology**: Screens for penny-stock momentum candidates, deep-triages them, opens positions on catalysts, and manages staged (tiered) exits intraday
- **Data Sources**: Market data + `StockScreener`, social/news catalysts
- **Instrument Selection**: Expert-driven (`can_recommend_instruments=True`, `should_expand_instrument_jobs=False`)
- **Key Features**:
  - **Self-executing**: places and manages its own orders — does **not** use the SmartRiskManager (`uses_risk_manager=False`) and creates **no** `ExpertRecommendation` records (order/expert attribution flows through `Transaction.expert_id`)
  - Screener-based candidate universe with confidence-weighted position sizing
  - Tiered take-profit / stop exits, wash-trade-safe exit staging
- **Settings**: numerous (screener filters, triage thresholds, tier/exit configuration) — configure in the Expert Settings UI.

### 7. FactorRanker
**Configurable cross-sectional multi-factor equity ranker (momentum / value / quality / PEAD)**

- **Type**: Systematic factor / portfolio expert (self-executing)
- **Methodology**: Ranks a candidate universe each rebalance by a weighted blend of factors and holds the long-only top slice
- **Data Sources**: FMP daily prices, income/balance/cash-flow statements, company profile, earnings; `StockScreener` for screener universes
- **Instrument Selection**: Expert-driven — one **batch run per rebalance** (`should_expand_instrument_jobs=False`)
- **Key Features**:
  - Factors: **momentum** (12-1), **value** (E/P + FCF/EV), **quality** (ROE + gross profitability − accruals), **PEAD** (post-earnings-announcement drift / SUE)
  - Universe from static `enabled_instruments` *or* the `StockScreener` (`universe_source`)
  - Long-only top-N construction (equal or score weighting, per-name cap, gross exposure)
  - **Self-rebalancing** via `FactorPortfolioManager` (diffs targets vs holdings → buy/sell deltas) — **no `ExpertRecommendation`, no SmartRiskManager** (`uses_risk_manager=False`)
  - Renders **only** the Enter-Market schedule (`schedules_open_positions=False`); supports weekly *or* monthly (Nth-weekday) schedules

**Key Settings**: `universe_source`, `factor_weight_momentum` / `factor_weight_value` / `factor_weight_quality` / `factor_weight_pead`, `top_n`, `weighting`, `max_weight_per_name`, `gross_exposure`, `winsorize_pct`, `pead_drift_window_days`, `min_price` (+ `screener_*` when `universe_source=screener`).

📖 **Full guide:** [docs/FACTORRANKER_EXPERT.md](docs/FACTORRANKER_EXPERT.md)

## Expert Properties Comparison

| Expert | Can Recommend Instruments | Self-executing¹ | Typical Use Case |
|--------|---------------------------|-----------------|------------------|
| TradingAgents | No | No | Complex AI-driven analysis with debate system |
| FinnHubRating | No | No | Analyst consensus tracking |
| FMPRating | No | No | Price target analysis |
| FMPSenateTraderWeight | No | No | Sophisticated government trading analysis |
| FMPSenateTraderCopy | **Yes** | No | Simple government trade copying |
| PennyMomentumTrader | **Yes** | **Yes** | Live intraday penny-stock momentum |
| FactorRanker | **Yes** | **Yes** | Systematic multi-factor equity ranking |

¹ *Self-executing* experts place and manage their own orders via a dedicated manager (no `ExpertRecommendation`, no SmartRiskManager); order/expert attribution flows through `Transaction.expert_id`.

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
- **Used by**: FMPSenateTraderCopy, PennyMomentumTrader, FactorRanker (can recommend their own instruments)
- **Best for**: Autonomous trading systems that discover opportunities

### Screener Selection
- **Filter-Based**: Instruments resolved at run time from `StockScreener` filters (market cap, price, volume, …)
- **Used by**: PennyMomentumTrader; FactorRanker when `universe_source="screener"`
- **Best for**: Strategies that rank/trade a broad, dynamically-filtered universe

## Job Scheduling and Management

### Standard Experts
- Create scheduled jobs for each enabled instrument
- Follow traditional scheduling patterns
- Suitable for portfolio-based strategies

### Self-Managing Experts (FMPSenateTraderCopy, PennyMomentumTrader, FactorRanker)
- Can recommend their own instruments
- Use `should_expand_instrument_jobs: False` to run a single batch job (no per-symbol duplication)
- Run analysis and discover/rank trading opportunities autonomously
- Ideal for discovery-based and portfolio/factor strategies

### Weekly vs Monthly Schedules
- Each analysis schedule (Enter-Market, Open-Positions) can fire **weekly** (chosen days + time) or **monthly** on the **Nth weekday** (e.g. *1st Monday*, *3rd Tuesday*).
- Experts that handle entries and exits in a single batch run (e.g. FactorRanker) set `schedules_open_positions=False` and render only the Enter-Market schedule.
- **Tip:** when running many self-executing experts that hit the same data API, stagger their schedule times to avoid rate-limiting.

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
| PennyMomentumTrader | FMP_API_KEY (+ catalyst sources) | Financial Modeling Prep, social/news |
| FactorRanker | FMP_API_KEY | Financial Modeling Prep |

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
3. **Define Properties** (`get_expert_properties`): e.g. `can_recommend_instruments`, `should_expand_instrument_jobs`, `schedules_open_positions`, `uses_risk_manager` (set `False` for self-executing experts that manage their own orders)
4. **Register Expert**: Add to `ba2_trade_platform/modules/experts/__init__.py`
5. **Test Integration**: Verify settings, scheduling, and analysis functionality

For detailed implementation guidelines, see the existing expert implementations in `ba2_trade_platform/modules/experts/`.