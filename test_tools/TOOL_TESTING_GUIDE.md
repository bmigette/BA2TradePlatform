"""
TradingAgents Tool Testing Guide
===============================

The test_tool.py script allows you to test individual TradingAgents tools with custom parameters.

## Usage

```bash
python test_tool.py <tool_name> <timeframe> <lookback_days> [symbol] [date] [options]
```

## Available Tools

### 1. get_YFin_data_online ✅ WORKING
Retrieves stock price data from Yahoo Finance.

**Examples:**
```bash
# Daily data for AAPL, 7 days lookback
python test_tool.py get_YFin_data_online 1d 7 AAPL

# Hourly data for NVDA, 3 days lookback (recent dates only)
python test_tool.py get_YFin_data_online 1h 3 NVDA 2025-09-30

# Weekly data for TSLA, 30 days lookback
python test_tool.py get_YFin_data_online 1wk 30 TSLA
```

**Limitations:**
- Intraday data (1m, 5m, 15m, 30m, 1h) only available for last 60 days
- Market holidays and weekends may result in no data

### 2. get_stockstats_indicators_report ⚠️ REQUIRES CACHE
Technical indicators analysis (requires cached data files).

**Examples:**
```bash
# RSI indicator (default)
python test_tool.py get_stockstats_indicators_report 1d 30 AAPL --indicator rsi

# MACD indicator with online mode
python test_tool.py get_stockstats_indicators_report 1h 30 MSFT --indicator macd --online

# Bollinger Bands
python test_tool.py get_stockstats_indicators_report 1d 20 GOOGL --indicator boll
```

**Available Indicators:**
- rsi, macd, macds, macdh
- boll, boll_ub, boll_lb
- close_50_sma, close_200_sma, close_10_ema
- atr, vwma

### 3. get_finnhub_news ⚠️ REQUIRES API KEY
Company news from Finnhub (requires API configuration).

**Examples:**
```bash
# Get news for AAPL, last 7 days
python test_tool.py get_finnhub_news 1d 7 AAPL

# Get news for MSFT, last 14 days
python test_tool.py get_finnhub_news 1d 14 MSFT 2025-09-30
```

### 4. get_reddit_news ⚠️ REQUIRES SETUP
Global news from Reddit (requires data directories and configuration).

### 5. get_finnhub_company_insider_sentiment ⚠️ REQUIRES API KEY
Insider sentiment data from Finnhub.

### 6. get_finnhub_company_insider_transactions ⚠️ REQUIRES API KEY
Insider transaction data from Finnhub.

## Timeframes

### Supported Values:
- **1m, 5m, 15m, 30m**: Intraday (last 60 days only)
- **1h**: Hourly (last 2 years)
- **1d**: Daily (historical)
- **1wk, 1mo**: Weekly/Monthly (historical)

### Timeframe Examples by Strategy:

```bash
# Scalping Strategy (1-minute data)
python test_tool.py get_YFin_data_online 1m 1 SPY

# Day Trading (5-minute data)
python test_tool.py get_YFin_data_online 5m 3 QQQ

# Swing Trading (1-hour data)
python test_tool.py get_YFin_data_online 1h 7 AAPL

# Position Trading (daily data)
python test_tool.py get_YFin_data_online 1d 30 MSFT

# Long-term Analysis (weekly data)
python test_tool.py get_YFin_data_online 1wk 90 BRK-B
```

## Command Line Options

```bash
--list-tools          # Show available tools
--indicator <name>    # Technical indicator for stockstats tools
--online             # Use online mode for stockstats tools
```

## Working Examples

### Basic Stock Data Testing:
```bash
# Current NVDA daily data, 7 days
python test_tool.py get_YFin_data_online 1d 7 NVDA

# Apple hourly data, 3 days (recent dates)
python test_tool.py get_YFin_data_online 1h 3 AAPL 2025-09-30

# Tesla weekly data, 12 weeks
python test_tool.py get_YFin_data_online 1wk 84 TSLA
```

### Configuration Testing:
```bash
# Test different timeframes with same parameters
python test_tool.py get_YFin_data_online 1m 1 AAPL 2025-09-30
python test_tool.py get_YFin_data_online 5m 1 AAPL 2025-09-30
python test_tool.py get_YFin_data_online 15m 3 AAPL 2025-09-30
python test_tool.py get_YFin_data_online 1h 7 AAPL 2025-09-30
python test_tool.py get_YFin_data_online 1d 30 AAPL 2025-09-30
```

## Troubleshooting

### Common Issues:

1. **"No data found"**: 
   - Try recent dates (within 60 days for intraday)
   - Check if market was open on selected dates
   - Verify symbol spelling

2. **"File not found" (stockstats tools)**:
   - Use `--online` flag
   - Ensure market data cache is populated
   - Try different symbols

3. **API errors (Finnhub tools)**:
   - Configure API keys in TradingAgents settings
   - Check API rate limits

### Data Availability:

- **Intraday (1m-1h)**: Last 60 days only
- **Daily (1d)**: Historical data available
- **Weekly/Monthly**: Historical data available
- **Weekends/Holidays**: No market data

## Integration with TradingAgents

This test script verifies that:
1. ✅ Tools use configured timeframe from expert settings
2. ✅ None parameters fetch timeframe from config
3. ✅ Different timeframes produce different data granularity
4. ✅ Configuration flows correctly through the system

The script demonstrates the timeframe integration we implemented, showing how agents will receive appropriately scoped data based on their configured timeframe settings.
"""