# New Toolkit Quick Reference Guide

## Overview
This is a quick reference for the refactored TradingAgents toolkit that integrates directly with BA2 data providers.

## Toolkit Methods

### Market Data (Fallback Logic)

#### get_ohlcv_data
Get stock price data (Open, High, Low, Close, Volume).
- **Providers**: Tries OHLCV providers in order until one succeeds
- **Parameters**:
  - `symbol` - Stock ticker (e.g., 'AAPL')
  - `start_date` - Start date 'YYYY-MM-DD'
  - `end_date` - End date 'YYYY-MM-DD'
  - `interval` - Optional timeframe (e.g., '1d', '1h', '5m')
- **Returns**: Price data in markdown table format

#### get_indicator_data
Get technical indicator data (RSI, MACD, Bollinger Bands, etc.).
- **Providers**: Tries indicator providers in order until one succeeds
- **Parameters**:
  - `symbol` - Stock ticker
  - `indicator` - Indicator name (e.g., 'rsi', 'macd', 'boll')
  - `start_date` - Start date 'YYYY-MM-DD'
  - `end_date` - End date 'YYYY-MM-DD'
  - `interval` - Optional timeframe
- **Returns**: Indicator data in markdown format

### News Data (Multi-Provider Aggregation)

#### get_company_news
Get news articles about a specific company.
- **Providers**: Aggregates from ALL news providers
- **Parameters**:
  - `symbol` - Stock ticker
  - `end_date` - End date 'YYYY-MM-DD'
  - `lookback_days` - Optional days to look back (default: 30)
- **Returns**: News articles with provider attribution

#### get_global_news
Get global market and macroeconomic news.
- **Providers**: Aggregates from ALL news providers
- **Parameters**:
  - `end_date` - End date 'YYYY-MM-DD'
  - `lookback_days` - Optional days to look back (default: 30)
- **Returns**: News articles with provider attribution

### Insider Trading Data (Multi-Provider Aggregation)

#### get_insider_transactions
Get insider trading transactions.
- **Providers**: Aggregates from ALL insider providers
- **Parameters**:
  - `symbol` - Stock ticker
  - `end_date` - End date 'YYYY-MM-DD'
  - `lookback_days` - Optional days to look back (default: 90)
- **Returns**: Transaction data with provider attribution

#### get_insider_sentiment
Get aggregated insider sentiment metrics.
- **Providers**: Aggregates from ALL insider providers
- **Parameters**:
  - `symbol` - Stock ticker
  - `end_date` - End date 'YYYY-MM-DD'
  - `lookback_days` - Optional days to look back (default: 90)
- **Returns**: Sentiment metrics with provider attribution

### Fundamental Data (Multi-Provider Aggregation)

#### get_balance_sheet
Get company balance sheet data.
- **Providers**: Aggregates from ALL fundamentals providers
- **Parameters**:
  - `symbol` - Stock ticker
  - `frequency` - 'annual' or 'quarterly'
  - `end_date` - End date 'YYYY-MM-DD'
  - `lookback_periods` - Number of periods (default: 4)
- **Returns**: Balance sheet data with provider attribution

#### get_income_statement
Get company income statement data.
- **Providers**: Aggregates from ALL fundamentals providers
- **Parameters**:
  - `symbol` - Stock ticker
  - `frequency` - 'annual' or 'quarterly'
  - `end_date` - End date 'YYYY-MM-DD'
  - `lookback_periods` - Number of periods (default: 4)
- **Returns**: Income statement data with provider attribution

#### get_cashflow_statement
Get company cash flow statement data.
- **Providers**: Aggregates from ALL fundamentals providers
- **Parameters**:
  - `symbol` - Stock ticker
  - `frequency` - 'annual' or 'quarterly'
  - `end_date` - End date 'YYYY-MM-DD'
  - `lookback_periods` - Number of periods (default: 4)
- **Returns**: Cash flow data with provider attribution

### Macroeconomic Data (Multi-Provider Aggregation)

#### get_economic_indicators
Get economic indicators (GDP, unemployment, inflation, etc.).
- **Providers**: Aggregates from ALL macro providers
- **Parameters**:
  - `end_date` - End date 'YYYY-MM-DD'
  - `lookback_days` - Optional days to look back (default: 365)
  - `indicators` - Optional list of specific indicators
- **Returns**: Economic data with provider attribution

#### get_yield_curve
Get Treasury yield curve data.
- **Providers**: Aggregates from ALL macro providers
- **Parameters**:
  - `end_date` - End date 'YYYY-MM-DD'
  - `lookback_days` - Optional days to look back (default: 90)
- **Returns**: Yield curve data with provider attribution

#### get_fed_calendar
Get Federal Reserve calendar and meetings.
- **Providers**: Aggregates from ALL macro providers
- **Parameters**:
  - `end_date` - End date 'YYYY-MM-DD'
  - `lookback_days` - Optional days to look back (default: 180)
- **Returns**: Fed calendar with provider attribution

## Tool Node Categories

### Market Tools
- `get_ohlcv_data` - Price data
- `get_indicator_data` - Technical indicators

### Social Tools
- `get_company_news` - Company-specific news and social sentiment

### News Tools
- `get_global_news` - Global market news

### Fundamentals Tools
- `get_balance_sheet` - Balance sheet statements
- `get_income_statement` - Income statements
- `get_cashflow_statement` - Cash flow statements
- `get_insider_transactions` - Insider trades
- `get_insider_sentiment` - Insider sentiment metrics

### Macro Tools
- `get_economic_indicators` - Economic data
- `get_yield_curve` - Treasury yields
- `get_fed_calendar` - Fed meetings and calendar

## Provider Configuration

### Setting Vendor Preferences
Configure providers in expert instance settings:

```python
{
    "vendor_news": ["eodhd", "polygon"],           # News providers
    "vendor_insider_transactions": ["eodhd"],       # Insider data
    "vendor_balance_sheet": ["eodhd", "fmp"],      # Fundamentals
    "vendor_stock_data": ["eodhd"],                 # OHLCV data
    "vendor_indicators": ["eodhd"]                  # Technical indicators
}
```

### Supported Providers

#### OHLCV Providers
- `eodhd` - EODHD Historical Data
- `polygon` - Polygon.io
- `fmp` - Financial Modeling Prep
- `alphavantage` - Alpha Vantage
- `yfinance` - Yahoo Finance

#### Indicator Providers
- `eodhd` - EODHD Technical Indicators
- `fmp` - FMP Technical Indicators
- `alphavantage` - Alpha Vantage Indicators

#### News Providers
- `eodhd` - EODHD News API
- `polygon` - Polygon News
- `finnhub` - Finnhub News
- `fmp` - FMP News

#### Fundamentals Providers
- `eodhd` - EODHD Fundamentals
- `fmp` - FMP Fundamentals
- `alphavantage` - Alpha Vantage Fundamentals

#### Insider Trading Providers
- `eodhd` - EODHD Insider Transactions
- `fmp` - FMP Insider Trading

#### Macro Providers
- `fred` - Federal Reserve Economic Data (default)

## Multi-Provider Results Format

### Aggregated Results
When multiple providers are configured, results are combined with markdown section headers:

```markdown
## EODHD

[Data from EODHD provider]

---

## Polygon

[Data from Polygon provider]

---

## Finnhub

[Data from Finnhub provider]
```

### Error Handling
If a provider fails, error is included in results:

```markdown
## EODHD

[Data from EODHD]

---

## Polygon

Error: API rate limit exceeded

---

## FMP

[Data from FMP]
```

## Fallback Results Format

### Successful Fallback
Only the first successful provider's data is returned:

```markdown
[Data from first successful provider]
```

### All Providers Failed
Error message includes all attempted providers:

```
Error: All OHLCV providers failed for AAPL
- EODHD: Connection timeout
- Polygon: API key invalid
- FMP: Symbol not found
```

## Usage Examples

### Example 1: Get Stock Price Data
```python
# Toolkit will try OHLCV providers in order
result = toolkit.get_ohlcv_data(
    symbol="AAPL",
    start_date="2024-01-01",
    end_date="2024-01-15",
    interval="1d"
)
```

### Example 2: Get Company News
```python
# Toolkit will aggregate from all news providers
result = toolkit.get_company_news(
    symbol="TSLA",
    end_date="2024-01-15",
    lookback_days=7
)
```

### Example 3: Get Financial Statements
```python
# Toolkit will aggregate from all fundamentals providers
balance_sheet = toolkit.get_balance_sheet(
    symbol="MSFT",
    frequency="quarterly",
    end_date="2024-01-15",
    lookback_periods=4
)

income_stmt = toolkit.get_income_statement(
    symbol="MSFT",
    frequency="quarterly",
    end_date="2024-01-15",
    lookback_periods=4
)
```

### Example 4: Get Economic Data
```python
# Toolkit will aggregate from all macro providers
result = toolkit.get_economic_indicators(
    end_date="2024-01-15",
    lookback_days=365,
    indicators=["GDP", "UNRATE", "CPIAUCSL"]
)
```

## Automatic Configuration Settings

The toolkit automatically uses configured lookback periods:

- **News tools**: Use `news_lookback_days` setting (default: 30 days)
- **Market data tools**: Use `market_history_days` setting (default: 90 days)
- **Fundamental tools**: Use `economic_data_days` setting via `lookback_periods` (default: 4 periods)
- **Insider tools**: Use `economic_data_days` setting (default: 90 days)
- **Macro tools**: Use `economic_data_days` setting (default: 365 days)

You can override these defaults by explicitly passing the lookback parameter.

## Best Practices

### 1. Configure Multiple Providers
For reliability, configure backup providers:
```python
{
    "vendor_news": ["eodhd", "polygon", "finnhub"],  # 3 providers for redundancy
    "vendor_stock_data": ["eodhd", "polygon"]        # Fallback if EODHD fails
}
```

### 2. Use Appropriate Lookback Periods
- **Intraday analysis**: 1-7 days
- **Short-term swing trading**: 7-30 days
- **Medium-term position trading**: 30-90 days
- **Long-term trend analysis**: 90-365 days

### 3. Monitor Provider Performance
Check logs for provider warnings and errors to identify unreliable providers.

### 4. Test Provider API Keys
Ensure all configured providers have valid API keys before running analysis.

### 5. Use Specific Indicators
When requesting economic indicators, specify exact indicator names for better results:
```python
indicators=["GDP", "UNRATE", "CPIAUCSL", "DGS10"]  # Specific indicators
```

## Troubleshooting

### Issue: No data returned
**Cause**: All providers failed or no providers configured
**Solution**: Check provider configuration and API keys in expert settings

### Issue: Partial data from some providers
**Cause**: Some providers have data gaps or API limits
**Solution**: Configure additional providers for better coverage

### Issue: Slow performance
**Cause**: Multiple providers being queried sequentially
**Solution**: This is expected for multi-provider aggregation; consider reducing number of providers for time-sensitive operations

### Issue: API rate limits
**Cause**: Too many requests to single provider
**Solution**: Distribute load across multiple providers or implement caching

## Migration from Old Toolkit

### Old Method → New Method Mapping

| Old Method | New Method | Change Type |
|------------|------------|-------------|
| `get_YFin_data_online` | `get_ohlcv_data` | Renamed + multi-provider |
| `get_stockstats_indicators_report_online` | `get_indicator_data` | Renamed + multi-provider |
| `get_stock_news_openai` | `get_company_news` | Renamed + aggregation |
| `get_global_news_openai` | `get_global_news` | Renamed + aggregation |
| `get_finnhub_company_insider_transactions` | `get_insider_transactions` | Renamed + aggregation |
| `get_finnhub_company_insider_sentiment` | `get_insider_sentiment` | Renamed + aggregation |
| `get_simfin_balance_sheet` | `get_balance_sheet` | Renamed + aggregation |
| `get_simfin_income_stmt` | `get_income_statement` | Renamed + aggregation |
| `get_simfin_cashflow` | `get_cashflow_statement` | Renamed + aggregation |
| `get_economic_calendar` | `get_fed_calendar` | Renamed + aggregation |
| `get_treasury_yield_curve` | `get_yield_curve` | Renamed + aggregation |
| `get_reddit_stock_info` | **REMOVED** | Use `get_company_news` |
| `get_reddit_news` | **REMOVED** | Use `get_global_news` |
| `get_fred_series_data` | **REMOVED** | Use `get_economic_indicators` |
| `get_inflation_data` | **REMOVED** | Use `get_economic_indicators` |
| `get_employment_data` | **REMOVED** | Use `get_economic_indicators` |

---

**Document Version**: 1.0  
**Last Updated**: 2024-01-15  
**For**: TradingAgents v2.0+ with BA2 provider integration
